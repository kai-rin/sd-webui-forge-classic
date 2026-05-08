# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Stable Diffusion WebUI Forge Neo** — a continuation of lllyasviel's Forge WebUI, focused on running latest diffusion models (Flux, Wan, SDXL, SD1.5, Chroma, Lumina, etc.) via a Gradio UI. Fork maintained by Haoming02. Branch: `neo`. Version string: `"neo"` in `modules_forge/forge_version.py`.

## Running the Application

```bash
# Standard launch (Windows only — no Unix .sh scripts)
webui-user.bat

# Direct launch (with venv already activated)
python launch.py

# Skip package installation (faster restart during dev)
python launch.py --skip-install

# Skip all environment preparation
python launch.py --skip-prepare-environment

# API-only mode (no Gradio UI)
# Note: --nowebui skips setup_progress_api() — custom endpoints (/internal/*) and heartbeat override are unavailable
python launch.py --nowebui

# See all flags
python launch.py --help
```

The startup chain: `webui-user.bat` → `webui.bat` (venv setup) → `python launch.py` → `modules/launch_utils.py:prepare_environment()` (pip installs) → `modules/launch_utils.py:start()` → `webui.py` (Gradio/FastAPI server).

**No test suite exists.** No pytest, no test runner, no CI tests.

## Linting / Formatting

Configured in `pyproject.toml`:
- **Black**: `line-length = 1024` (effectively no line wrapping)
- **Ruff**: `target-version = "py311"`, excludes `extensions/`, rules: `N805, S307, S102, T, W, F`

```bash
# Format
black .

# Lint
ruff check .
ruff check --fix .  # auto-fix
```

## Pinned Versions

- Python: **3.13.x** (checked on startup, warns on mismatch)
- PyTorch: **2.10.0+cu130** (CUDA 13.0)
- Gradio: **4.40.0**
- Key optional packages: xformers 0.0.34, sageattention 2.2.0, flash_attn 2.8.3, nunchaku 1.2.1, bitsandbytes 0.49.1

## Architecture

### Three-Layer Structure

1. **`backend/`** — ComfyUI-derived inference engine (pure model inference)
   - `memory_management.py` — VRAM state machine and model loading/offloading
   - `loader.py` — Model detection via `huggingface_guess`; dispatches to `ForgeDiffusionEngine` subclasses
   - `operations.py` — Tensor ops, LoRA merging, SDPA backends
   - `attention.py` — Attention dispatch: SageAttention → FlashAttention → xformers → PyTorch SDPA → basic
   - `sampling/` — Denoising loop, k-diffusion wrappers, noise prediction types
   - `patcher/` — ModelPatcher (from ComfyUI) for LoRA, ControlNet weight patching
   - `diffusion_engine/` — Per-architecture engine subclasses (StableDiffusion, Flux, Wan, QwenImage, etc.)
   - `args.py` — `dynamic_args` global mutable dict for passing inference-time params across modules

2. **`modules_forge/`** — Forge-specific bridge, patches, and UI additions
   - `initialization.py` — CUDA init, GPU warmup, tokenizer decompression
   - `main_thread.py` — **Single-threaded task queue** (all major inference serialized on main thread; Gradio runs in a daemon thread)
   - `main_entry.py` — Checkpoint manager UI, model selection, preset handling
   - `patch_basic.py` — Monkey-patches gradio (SSE stream recovery, heartbeat disable, queue cleanup), safetensors, torch.load
   - `uv_hook.py` — Monkey-patches `subprocess.run` to redirect pip → uv
   - `presets.py` — Per-architecture defaults (sampler, scheduler, steps, CFG) for sd/xl/flux/klein/qwen/lumina/zit/wan/anima
   - `config.py` — `always_disabled_extensions` blocklist
   - `packages/` — Vendored packages: `comfy/`, `gguf/`, `huggingface_guess/`, `k_diffusion/`

3. **`modules/`** — A1111-heritage core
   - `launch_utils.py` — Environment preparation, package installation
   - `processing.py` — Core generation pipeline (`StableDiffusionProcessing`)
   - `ui.py` — Gradio UI construction (`create_ui()`)
   - `scripts.py` — Script/extension plugin system (`Script` base class)
   - `script_callbacks.py` — Event hook system (before_launch, before_ui, app_started, cfg_denoiser, image_saved, etc.)
   - `shared.py` — Global state container (opts, demo, sd_model, state)
   - `sd_models.py` — Checkpoint discovery and loading
   - `cmd_args.py` — CLI argument definitions
   - `progress.py` — Progress polling, diagnostic endpoints (`/internal/debug-state`, `/internal/close-session`), heartbeat route override

### Main Thread Model

The main thread runs `main_thread.loop()` (blocking deque consumer). All GPU-touching inference is dispatched to this queue. Gradio runs in a separate daemon thread. This prevents race conditions on VRAM model movement.

### Extension System

- **`extensions-builtin/`** — Always loaded (ControlNet, LoRA, IPAdapter, MultiDiffusion, Compile, etc.)
- **`extensions/`** — User-installed, respects `disabled_extensions` in `config.json`
- Extensions provide `Script` subclasses in their `scripts/` subdirectory
- Each extension can have an `install.py` (run at startup) and `metadata.ini` (ordering hints)
- Callback hooks registered via `modules/script_callbacks.py`
- Third-party `sd-webui-controlnet` and `multidiffusion-upscaler` are force-disabled (conflicts with built-in versions)

### Extension Development (Fork Pattern)
- Extensions under `extensions/` may be forked repos with `origin` (fork) + `upstream` (source) remotes
- Custom branches (e.g., `custom-main`) track `origin`, sync via: `git fetch upstream && git checkout main && git pull upstream main && git checkout custom-main && git rebase main`
- Run `/extension-compat-check extensions/<name>` before first launch with a new extension

### Supported Model Architectures

Defined in `backend/loader.py`: StableDiffusion (SD1.5), StableDiffusionXL, StableDiffusionXLRefiner, Flux, Flux2 (Klein), Wan, QwenImage, Lumina2, ZImage, Chroma, Anima. Detection uses `huggingface_guess` (inspects state dict keys).

### Configuration Files (runtime-generated)

| File | Purpose |
|------|---------|
| `config.json` | All UI settings, preset checkpoint/module selections |
| `ui-config.json` | Gradio component defaults |
| `styles.csv` | Saved prompt styles |

**Preset vs ui-config.json**: `presets.py` defines per-architecture defaults (sampler, steps, CFG). `main_entry.py:forge_main_entry()` registers `root_block.load` (page load) and `ui_forge_preset.change` (explicit switch) handlers. Page-load handler (`on_preset_load`) only sets checkpoint/VAE/dtype and UI structure — sampler/steps/CFG are skipped (`gr.skip()`) to preserve ui-config.json defaults. The explicit switch handler (`on_preset_change`) applies all values.

## Key Development Patterns

- **No git-cloning at runtime** — deliberate optimization over original A1111
- **`webui-user.bat`** contains user-specific paths and ports; marked `assume-unchanged` in git — do not commit changes to this file
- **`dynamic_args`** (`backend/args.py`) is a global mutable dict used to pass model-type flags (kontext, wan, edit, etc.) across module boundaries during inference
- **Restart mechanism**: writing `tmp/restart` triggers graceful UI reload via `SD_WEBUI_RESTART` env var
- **Environment variables** can override torch version (`TORCH_COMMAND`, `TORCH_INDEX_URL`), gradio version (`GRADIO_PACKAGE`), and requirements file (`REQS_FILE`)
- **`--uv` flag** monkey-patches subprocess.run to redirect all pip calls to uv pip
- **`on_before_launch` callback** — fires after `create_ui()`/`queue()` but before `launch()`, so extensions can register Gradio event handlers that appear in the initial config served to browsers (avoids race condition with `on_app_started`)
- **`image_saved_callback` gotcha** — `save_image()` は `image_saved_callback` を同期発火する。拡張が `shared.opts.grid_save=True` 等を一時的に強制すると、Eagle 等の他拡張のコールバックも意図せず発火する。コールバックを避けるには PIL の `.save()` で直接保存する
- **Starlette Route patching**: `route.endpoint` alone is insufficient; must also set `route.app = request_response(new_endpoint)` because Starlette caches the ASGI app at construction time
- **SSE session cleanup**: `javascript/sseMonitor.js` sends `sendBeacon('/internal/close-session')` on `beforeunload` to prevent stale session accumulation (HTTP/1.1 max 6 connections per origin)
- **Gradio heartbeat disabled**: `/heartbeat/{session_hash}` replaced with non-streaming noop to free persistent connections for multi-tab use
- **Gradio `queue=False` convention**: After `demo.queue()`, all handlers default to `queue=True` (SSE via `/queue/data`). Non-GPU handlers (settings, visibility toggles, HTML generation, JS-only) **must** set `queue=False` to use direct HTTP POST. Missing `queue=False` on `.load()` handlers causes each page load to hold an SSE connection, exhausting HTTP/1.1's 6-connection limit with multiple tabs. Convention: if a handler doesn't touch GPU or `queue_lock`, it needs `queue=False`.
- **Temporary files** (screenshots, debug output, etc.) go in `.claude/tmp/` (gitignored), not the project root
- **JS file caching**: Gradio serves `javascript/*.js` with `?{mtime}` query — editing a JS file requires server restart for the new version to be served. Simply reloading the page is insufficient if the server process is the same
