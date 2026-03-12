import os
import time
import warnings
from functools import wraps
from pathlib import Path

import gradio.networking
import httpx
import safetensors.torch
import torch
from tqdm import tqdm

from modules.errors import display


def gradio_url_ok_fix(url: str) -> bool:
    try:
        for _ in range(5):
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore")
                r = httpx.head(url, timeout=999, verify=False)
            if r.status_code in (200, 401, 302):
                return True
            time.sleep(0.500)
    except (ConnectionError, httpx.ConnectError):
        return False
    return False


def build_loaded(module, loader_name):
    original_loader_name = f"{loader_name}_origin"

    if not hasattr(module, original_loader_name):
        setattr(module, original_loader_name, getattr(module, loader_name))

    original_loader = getattr(module, original_loader_name)

    @wraps(original_loader)
    def loader(*args, **kwargs):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter(action="ignore", category=FutureWarning)
                return original_loader(*args, **kwargs)
        except Exception as e:
            display(e, f"{module.__name__}.{loader_name}")

            exc = "\n"
            for path in list(args) + list(kwargs.values()):
                if isinstance(path, str) and os.path.isfile(path):
                    exc += f'Failed to read file "{path}"\n'
                    backup_file = f"{path}.corrupted"
                    if os.path.exists(backup_file):
                        os.remove(backup_file)
                    os.replace(path, backup_file)
                    exc += f'Forge has moved the corrupted file to "{backup_file}"\n'
                    exc += "Please try downloading the model again\n"
            print(exc)
            raise ValueError from None

    setattr(module, loader_name, loader)


def always_show_tqdm(*args, **kwargs):
    kwargs["disable"] = False
    if "name" in kwargs:
        del kwargs["name"]
    return tqdm(*args, **kwargs)


def long_path_prefix(path: Path) -> Path:
    if os.name == "nt" and not str(path).startswith("\\\\?\\") and not path.exists():
        return Path("\\\\?\\" + str(path))
    return path


def patch_gradio_sse_stream():
    """Fix Gradio 4.40 SSE stream bug: onerror handler doesn't reset stream_status.open.

    When the SSE stream (/queue/data) drops (e.g. browser timeout during long tasks),
    Gradio's open_stream() sets stream_status.open=true but the onerror handler never
    resets it to false. This causes subsequent submit() calls to skip reopening the
    stream, breaking all Gradio function calls (Generate results, UI updates, etc.).

    This patch adds `n.open=!1;` to the onerror handler in the minified JS so the
    stream is properly marked as closed on error, allowing auto-recovery.
    """
    import gradio
    assets_dir = Path(gradio.__file__).parent / "templates" / "frontend" / "assets"
    if not assets_dir.exists():
        return

    for js_file in assets_dir.glob("index-*.js"):
        try:
            content = js_file.read_text(encoding="utf-8")
        except Exception:
            continue

        # Match the onerror handler in open_stream (minified as Kt)
        # Original: a.onerror=async function(){await Promise.all(Object.keys(t).map(...
        # Patched:  a.onerror=async function(){n.open=!1;await Promise.all(Object.keys(t).map(...
        # where 'n' is stream_status and 'a' is the EventSource stream
        old = '.onerror=async function(){await Promise.all(Object.keys(t).map'
        new = '.onerror=async function(){n.open=!1;await Promise.all(Object.keys(t).map'

        if old in content and new not in content:
            content = content.replace(old, new)
            js_file.write_text(content, encoding="utf-8")
            print(f"[Forge] Patched Gradio SSE stream recovery in {js_file.name}")
        elif new in content:
            pass  # Already patched
        # else: pattern not found, different Gradio version — skip silently


def patch_gradio_queue_cleanup():
    """Fix memory leak: clean_events() never removes from pending_event_ids_session.

    Gradio's Queue.clean_events() marks active jobs as not alive and removes events
    from queues, but never cleans up the pending_event_ids_session dict. This causes
    it to grow unbounded over the server's lifetime.
    """
    from gradio.queueing import Queue

    original_clean_events = Queue.clean_events

    async def patched_clean_events(self, *, session_hash=None, event_id=None):
        await original_clean_events(self, session_hash=session_hash, event_id=event_id)
        if session_hash and session_hash in self.pending_event_ids_session:
            del self.pending_event_ids_session[session_hash]

    Queue.clean_events = patched_clean_events


def patch_all_basics():
    import logging

    from huggingface_hub import file_download

    file_download.tqdm = always_show_tqdm
    file_download.logger.setLevel(logging.ERROR)

    from huggingface_hub.file_download import _download_to_tmp_and_move as original_download_to_tmp_and_move

    @wraps(original_download_to_tmp_and_move)
    def patched_download_to_tmp_and_move(incomplete_path: Path, destination_path: Path, *args, **kwargs):
        incomplete_path = long_path_prefix(incomplete_path)
        destination_path = long_path_prefix(destination_path)
        return original_download_to_tmp_and_move(incomplete_path, destination_path, *args, **kwargs)

    file_download._download_to_tmp_and_move = patched_download_to_tmp_and_move

    gradio.networking.url_ok = gradio_url_ok_fix
    build_loaded(safetensors.torch, "load_file")
    build_loaded(torch, "load")

    patch_gradio_sse_stream()
    patch_gradio_queue_cleanup()
