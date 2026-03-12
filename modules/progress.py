from __future__ import annotations

import base64
import io
import random
import string
import time
from collections import OrderedDict
from typing import List

import gradio as gr
from pydantic import BaseModel, Field

import modules.shared as shared
from modules.shared import opts

current_task = None
pending_tasks = OrderedDict()
finished_tasks = []
recorded_results = []
recorded_results_limit = 2


def start_task(id_task):
    global current_task

    current_task = id_task
    pending_tasks.pop(id_task, None)


def finish_task(id_task):
    global current_task

    if current_task == id_task:
        current_task = None

    finished_tasks.append(id_task)
    if len(finished_tasks) > 16:
        finished_tasks.pop(0)


def create_task_id(task_type):
    N = 7
    res = "".join(random.choices(string.ascii_uppercase + string.digits, k=N))
    return f"task({task_type}-{res})"


def record_results(id_task, res):
    recorded_results.append((id_task, res))
    if len(recorded_results) > recorded_results_limit:
        recorded_results.pop(0)


def add_task_to_queue(id_job):
    pending_tasks[id_job] = time.time()


class PendingTasksResponse(BaseModel):
    size: int = Field(title="Pending task size")
    tasks: List[str] = Field(title="Pending task ids")


class ProgressRequest(BaseModel):
    id_task: str = Field(default=None, title="Task ID", description="id of the task to get progress for")
    id_live_preview: int = Field(default=-1, title="Live preview image ID", description="id of last received last preview image")
    live_preview: bool = Field(default=True, title="Include live preview", description="boolean flag indicating whether to include the live preview image")


class ProgressResponse(BaseModel):
    active: bool = Field(title="Whether the task is being worked on right now")
    queued: bool = Field(title="Whether the task is in queue")
    completed: bool = Field(title="Whether the task has already finished")
    progress: float | None = Field(default=None, title="Progress", description="The progress with a range of 0 to 1")
    eta: float | None = Field(default=None, title="ETA in secs")
    live_preview: str | None = Field(default=None, title="Live preview image", description="Current live preview; a data: uri")
    id_live_preview: int | None = Field(default=None, title="Live preview image ID", description="Send this together with next request to prevent receiving same image")
    textinfo: str | None = Field(default=None, title="Info text", description="Info text used by WebUI.")


def debug_state():
    """Diagnostic endpoint to inspect server-side state for debugging SSE/queue issues."""
    from modules.call_queue import queue_lock
    from modules_forge import main_thread

    lock_status = "free"
    if not queue_lock.acquire(blocking=False):
        lock_status = "locked"
    else:
        queue_lock.release()

    gradio_sessions = -1
    pending_event_ids_count = -1
    gradio_sessions_error = None
    try:
        if shared.demo is not None and hasattr(shared.demo, '_queue') and shared.demo._queue is not None:
            queue = shared.demo._queue
            gradio_sessions = len(queue.pending_messages_per_session)
            pending_event_ids_count = len(queue.pending_event_ids_session)
    except Exception as e:
        gradio_sessions_error = f"{type(e).__name__}: {e}"

    return {
        "current_task": current_task,
        "pending_tasks": list(pending_tasks.keys()),
        "finished_tasks": list(finished_tasks),
        "queue_lock": lock_status,
        "main_thread_waiting": len(main_thread.waiting_queue),
        "main_thread_finished": len(main_thread.finished_tasks),
        "gradio_sse_sessions": gradio_sessions,
        "gradio_pending_event_ids": pending_event_ids_count,
        "gradio_sessions_error": gradio_sessions_error,
    }


def close_session(session_hash: str = ""):
    """Remove a Gradio SSE session from the queue cache.

    Called via navigator.sendBeacon() from sseMonitor.js on tab close/reload.
    Prevents stale session accumulation which exhausts the browser's HTTP/1.1
    per-origin connection limit (6) when many tabs are open simultaneously.
    """
    if not session_hash:
        return {"status": "error", "detail": "missing session_hash"}
    try:
        if shared.demo is not None and hasattr(shared.demo, "_queue") and shared.demo._queue is not None:
            queue = shared.demo._queue
            removed = False
            if session_hash in queue.pending_messages_per_session:
                del queue.pending_messages_per_session[session_hash]
                removed = True
            queue.pending_event_ids_session.pop(session_hash, None)
            return {"status": "ok", "removed": removed, "session_hash": session_hash}
    except Exception as e:
        return {"status": "error", "detail": f"{type(e).__name__}: {e}"}
    return {"status": "ok", "removed": False, "session_hash": session_hash}


def _override_heartbeat_route(app):
    """Replace Gradio's persistent heartbeat SSE with a non-streaming response.

    Gradio 4.40 opens a permanent SSE connection per tab via /heartbeat/{session_hash},
    sending ALIVE every 15s. With HTTP/1.1's 6-connection-per-origin limit, 7+ tabs
    exhaust all connection slots. Since this WebUI doesn't use .unload() handlers,
    we can safely replace it with a single response that completes immediately.

    The Gradio client sets this.heartbeat_event = this.stream(url) once and never
    retries if it's already set, so the client won't reconnect after the response ends.
    """
    from starlette.responses import PlainTextResponse
    from starlette.routing import request_response

    async def heartbeat_noop(session_hash: str):
        return PlainTextResponse("data: ALIVE\n\n", media_type="text/event-stream")

    for route in app.routes:
        if hasattr(route, "path") and route.path == "/heartbeat/{session_hash}":
            route.endpoint = heartbeat_noop
            route.app = request_response(heartbeat_noop)
            print("[Forge] Replaced Gradio persistent heartbeat with non-streaming version")
            break


def setup_progress_api(app):
    _override_heartbeat_route(app)
    app.add_api_route("/internal/pending-tasks", get_pending_tasks, methods=["GET"])
    app.add_api_route("/internal/debug-state", debug_state, methods=["GET"])
    app.add_api_route("/internal/close-session", close_session, methods=["POST"])
    return app.add_api_route("/internal/progress", progressapi, methods=["POST"], response_model=ProgressResponse)


def get_pending_tasks():
    pending_tasks_ids = list(pending_tasks)
    pending_len = len(pending_tasks_ids)
    return PendingTasksResponse(size=pending_len, tasks=pending_tasks_ids)


def progressapi(req: ProgressRequest):
    active = req.id_task == current_task
    queued = req.id_task in pending_tasks
    completed = req.id_task in finished_tasks

    if not active:
        textinfo = "Waiting..."
        if queued:
            sorted_queued = sorted(pending_tasks.keys(), key=lambda x: pending_tasks[x])
            queue_index = sorted_queued.index(req.id_task)
            textinfo = "In queue: {}/{}".format(queue_index + 1, len(sorted_queued))
        return ProgressResponse(active=active, queued=queued, completed=completed, id_live_preview=-1, textinfo=textinfo)

    progress = 0

    job_count, job_no = shared.state.job_count, shared.state.job_no
    sampling_steps, sampling_step = shared.state.sampling_steps, shared.state.sampling_step

    if job_count > 0:
        progress += job_no / job_count
    if sampling_steps > 0 and job_count > 0:
        progress += 1 / job_count * sampling_step / sampling_steps

    progress = min(progress, 1)

    elapsed_since_start = time.time() - shared.state.time_start
    predicted_duration = elapsed_since_start / progress if progress > 0 else None
    eta = predicted_duration - elapsed_since_start if predicted_duration is not None else None

    live_preview = None
    id_live_preview = req.id_live_preview

    if opts.live_previews_enable and req.live_preview:
        shared.state.set_current_image()
        if shared.state.id_live_preview != req.id_live_preview:
            image = shared.state.current_image
            if image is not None:
                _video: bool = getattr(image, "is_animated", False)
                _format = "gif" if _video else opts.live_previews_image_format
                buffered = io.BytesIO()

                if _format == "png":
                    # using optimize for large images takes an enormous amount of time
                    if max(*image.size) <= 256:
                        save_kwargs = {"optimize": True}
                    else:
                        save_kwargs = {"optimize": False, "compress_level": 1}
                elif _format == "gif":
                    save_kwargs = {"save_all": True, "loop": 0}
                else:
                    image = image.convert("RGB")
                    save_kwargs = {}

                image.save(buffered, format=_format, **save_kwargs)
                base64_image = base64.b64encode(buffered.getvalue()).decode("ascii")
                live_preview = f"data:image/{_format};base64,{base64_image}"
                id_live_preview = shared.state.id_live_preview

    return ProgressResponse(active=active, queued=queued, completed=completed, progress=progress, eta=eta, live_preview=live_preview, id_live_preview=id_live_preview, textinfo=shared.state.textinfo)


def restore_progress(id_task):
    while id_task == current_task or id_task in pending_tasks:
        time.sleep(0.1)

    res = next(iter([x[1] for x in recorded_results if id_task == x[0]]), None)
    if res is not None:
        return res

    return gr.skip(), gr.skip(), gr.skip(), f"Couldn't restore progress for {id_task}: results either have been discarded or never were obtained"
