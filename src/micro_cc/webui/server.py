"""FastAPI surface for the GUI. Chat routes, minus auth.

Everything here is bound to 127.0.0.1 and scoped to the single project_dir the
TUI handed over at /gui time, so there is no user id, no conversation id and no
tenancy — the process IS the session.
"""

import asyncio
import json
import os
import uuid
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from micro_cc.claude_loop_ import claude_loop, GATEABLE_TOOLS
from micro_cc.models import registry as model_registry
from micro_cc.utils import command_registry, hidden_prompts, hints, settings_store_
from micro_cc.utils.msg_store_ import (
    store_msgs,
    erase_msgs,
    erase_summary,
    erase_checkpoint,
    load_msgs,
    rewind_msgs,
)
from micro_cc.utils.tokenization_simple import erase_token_stats
from micro_cc.utils.project_files_ import list_project_files
from micro_cc.cache import state_store
from micro_cc.webui import bridge, history, session

DIST = Path(__file__).parent / "dist"

UPLOAD_SUBDIR = "uploads"

# Palette contents come from command_registry.py — the single source of
# truth also read by the TUI's autocomplete (start_live_.py) — filtered to
# drop TUI-only commands (gui=False: /exit, /update, /gui, /graph) and
# stripped of the gui flag itself, which the frontend has no use for.
COMMANDS = [
    {k: v for k, v in c.items() if k != "gui"}
    for c in command_registry.COMMANDS
    if c.get("gui", True)
]

# Hiding a command from the palette above doesn't stop someone typing it
# directly into the chat box — /api/chat's hidden_prompts.PROMPT_BODIES
# lookup is flag-blind, so enforce gui=False there too (see /graph's entry
# in command_registry.py for why it's excluded).
_GUI_ALLOWED_PROMPT_COMMANDS = {c["name"] for c in COMMANDS if c.get("kind") == "prompt"}

REWIND_LIMIT = 40

app = FastAPI(title="micro-cc")


# ── helpers ────────────────────────────────────────────────────────

def _project_dir() -> str:
    return session.STATE["project_dir"]


def _safe_path(rel: str) -> Path | None:
    """Resolve `rel` inside project_dir, or None if it escapes.

    The server is bound to loopback, but "only I can reach it" is not the same
    as "any path is fine" — a stray ../ in a rendered tool output would
    otherwise turn an <img> into an arbitrary file read.
    """
    root = Path(_project_dir()).resolve()
    target = (root / rel).resolve() if not os.path.isabs(rel) else Path(rel).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return None
    return target


def _latest_user_text(messages: list) -> str:
    for msg in reversed(messages or []):
        if msg.get("role") != "user":
            continue
        for part in msg.get("parts", []):
            if part.get("type") == "text":
                return part.get("text", "")
        return msg.get("content", "")
    return ""


def _ends_with_user_turn(messages: list) -> bool:
    return bool(messages) and messages[-1].get("role") == "user"


def _sse_response(gen):
    # These headers are not decoration: without x-vercel-ai-ui-message-stream
    # the SDK won't parse the body as a UI message stream at all.
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={
            "x-vercel-ai-ui-message-stream": "v1",
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _empty_stream():
    """A well-formed but empty assistant turn. Used when a POST arrives with
    nothing to do — e.g. the SDK auto-sending after a deny that /api/chat/deny
    has already fully resolved."""
    yield bridge.sse({"type": "start", "messageId": f"msg-{uuid.uuid4().hex[:12]}"})
    yield bridge.sse({"type": "finish", "finishReason": "stop"})
    yield bridge.SSE_DONE


async def _refusal_stream(text: str):
    """Same well-formed shape as _empty_stream, but with a visible reason —
    for a command this surface can't safely run (see _GUI_ALLOWED_PROMPT_COMMANDS)
    instead of silently no-op'ing or spending a model call on it."""
    yield bridge.sse({"type": "start", "messageId": f"msg-{uuid.uuid4().hex[:12]}"})
    yield bridge.sse({"type": "error", "errorText": text})
    yield bridge.sse({"type": "finish", "finishReason": "stop"})
    yield bridge.SSE_DONE


# ── session / settings ─────────────────────────────────────────────

@app.get("/api/session")
async def get_session():
    from importlib.metadata import version as pkg_version

    settings = {s["setting_key"]: s["setting_value"] for s in settings_store_.read_settings()}
    return {
        "session": {
            "project_dir": _project_dir(),
            "version": pkg_version("micro-cc"),
            "model": settings.get("model", model_registry.DEFAULT_MODEL),
            "models": model_registry.MODEL_OPTIONS,
            "dangerous": settings.get("dangerous", []),
            "gateable": GATEABLE_TOOLS,
            "tokens_budget": settings.get("tokens_budget", model_registry.DEFAULT_TRIM_BUDGET),
        },
        "commands": COMMANDS,
        # Same registry the TUI's hint bar rotates — utils/hints.py.
        "hints": hints.for_surface(hints.GUI),
    }


@app.get("/api/background-status")
async def get_background_status():
    """Background `cmd &` processes bash_ has left running — same self-scoped
    dict claude_loop_ feeds the model via <process-status>, exposed here for
    the header pill (see chat-header.tsx). Polled on an interval, so this is
    a plain in-memory dict lookup — no subprocess, no disk I/O."""
    from micro_cc.tools.bash_tool import list_background_processes

    return {"processes": list_background_processes()}


@app.post("/api/settings")
async def post_settings(request: Request):
    """Writes through to ~/.micro-cc/settings.json — the same file the TUI's
    pickers edit — so a change here survives the GUI closing."""
    data = await request.json()
    if "model" in data:
        settings_store_.edit_setting("model", data["model"])
        session.STATE["model"] = data["model"]
    if "dangerous" in data:
        settings_store_.edit_setting("dangerous", data["dangerous"])
        session.STATE["dangerous"] = set(data["dangerous"])
    return {"ok": True}


# ── history ────────────────────────────────────────────────────────

@app.get("/api/messages")
async def get_messages():
    return {"messages": history.to_ui_messages(load_msgs(_project_dir()))}


@app.get("/api/rewind")
async def get_rewind():
    msgs = load_msgs(_project_dir())
    options = [
        {"index": i, "label": m["content"].strip().replace("\n", " ")[:100]}
        for i, m in enumerate(msgs)
        if m.get("role") == "user"
        and isinstance(m.get("content"), str)
        and not m["content"].strip().startswith("<system-reminder>")
    ]
    return {"options": options[-REWIND_LIMIT:]}


@app.post("/api/rewind")
async def post_rewind(request: Request):
    data = await request.json()
    project_dir = _project_dir()
    msgs = load_msgs(project_dir)
    idx = data["index"]
    rewound_text = msgs[idx].get("content", "") if idx < len(msgs) else ""
    remaining = rewind_msgs(project_dir, idx)
    session.STATE["msgs"] = remaining
    return {
        "messages": history.to_ui_messages(remaining),
        "rewound_text": rewound_text,
    }


# ── files ──────────────────────────────────────────────────────────

@app.get("/api/files")
async def get_files():
    return {"files": list_project_files(_project_dir())}


@app.get("/api/file")
async def get_file(path: str):
    target = _safe_path(path)
    if target is None or not target.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(target)


@app.post("/api/upload")
async def post_upload(file: UploadFile = File(...)):
    """Attachments become real files under {project_dir}/uploads/ — the model
    reads them with its ordinary read_ tool (multimodal by extension, see
    file_tools_.py), so there's nothing to presign, cache or expire."""
    dest_dir = Path(_project_dir()) / UPLOAD_SUBDIR
    dest_dir.mkdir(parents=True, exist_ok=True)

    safe_name = os.path.basename(file.filename or "upload")
    dest = dest_dir / safe_name
    stem, suffix = dest.stem, dest.suffix
    n = 1
    while dest.exists():
        dest = dest_dir / f"{stem}_{n}{suffix}"
        n += 1

    dest.write_bytes(await file.read())
    return {"path": str(dest.relative_to(Path(_project_dir())))}


# ── commands ───────────────────────────────────────────────────────

@app.post("/api/command")
async def post_command(request: Request):
    data = await request.json()
    name = data.get("name")
    project_dir = _project_dir()

    if name == "/clear":
        erase_msgs(project_dir)
        erase_summary(project_dir)
        erase_checkpoint(project_dir)
        erase_token_stats(project_dir)
        state_store.clear_todos(project_dir)
        session.STATE["msgs"] = []
        return {"cleared": True, "message": "all messages erased"}

    if name == "/author":
        return {"message": "George Juraj Salapa — https://gsequist.github.io/"}

    return {"message": f"{name} is not available here"}


# ── chat ───────────────────────────────────────────────────────────

@app.post("/api/chat")
async def post_chat(request: Request):
    data = await request.json()
    messages = data.get("messages", [])
    project_dir = _project_dir()

    # A pending approval means claude_loop is parked on a `yield` inside a
    # generator we still hold. This POST is the resume, not a new turn — the
    # SDK sends it with no fresh user message, driven by sendAutomaticallyWhen.
    resuming = session.STATE["pending"] is not None

    if not resuming and not _ends_with_user_turn(messages):
        return _sse_response(_empty_stream())

    session.STATE["stop"].clear()
    msg_id = f"msg-{uuid.uuid4().hex[:12]}"

    if resuming:
        # An approval resumes with True. A question resumes with the
        # {header: answer} dict /api/chat/answer stashed — passing True there
        # would hand the model a boolean as the answer to every question.
        if session.STATE["pending_kind"] == "question":
            session.resolve_pending(session.STATE["answers"])
        else:
            session.resolve_pending(True)
        session.clear_pending()
        gen = session.STATE["gen"]
    else:
        query = _latest_user_text(messages)

        # /setup & friends: same hidden turn the TUI pushes, from the same
        # source (utils/hidden_prompts.py).
        head, _, rest = query.strip().partition(" ")
        if head in hidden_prompts.PROMPT_BODIES:
            if head not in _GUI_ALLOWED_PROMPT_COMMANDS:
                return _sse_response(_refusal_stream(
                    f"{head} is TUI-only — run it from the terminal instead. "
                    "(Subagent monitoring/wakeup only reach the terminal app, "
                    "not this browser session — see /graph's entry in "
                    "command_registry.py for why.)"
                ))
            query = hidden_prompts.build(head, rest.strip())

        # Attachments are already on disk; name them so the model knows to look.
        files = data.get("files") or []
        if files:
            listed = "\n".join(f"- {f}" for f in files)
            query = f"Files attached to this message (relative to the project directory):\n{listed}\n\n{query}"

        msgs = load_msgs(project_dir)
        session.STATE["msgs"] = msgs
        gen = claude_loop(
            query=query,
            msgs=msgs,
            project_dir=project_dir,
            model=settings_store_.get_setting("model") or model_registry.DEFAULT_MODEL,
            max_tokens=session.STATE["trim_budget"],
            dangerous_tools=set(settings_store_.get_setting("dangerous") or []),
            surface="gui",
        )
        session.STATE["gen"] = gen

    async def run():
        # One turn at a time — a second POST waits rather than interleaving two
        # writers into the same msgs list.
        async with session.STATE["lock"]:
            try:
                async for chunk in bridge.stream(gen, msg_id, project_dir):
                    yield chunk
                    if session.STATE["stop"].is_set():
                        break
            finally:
                # Only tear the generator down if it isn't parked on an
                # approval — that's the one case where it must outlive the
                # response.
                if session.STATE["pending"] is None:
                    session.STATE["gen"] = None
                    await gen.aclose()
                    store_msgs(project_dir, session.STATE["msgs"])

    return _sse_response(run())


@app.post("/api/chat/answer")
async def post_answer(request: Request):
    """Stash the answers to an ask_user_question_tool_ prompt.

    Doesn't resume the loop itself — the browser follows this with the SDK's
    approval response, which triggers the /api/chat re-POST that streams the
    rest of the turn. Keeping the two separate means the answers are already
    in place by the time the resume lands, whatever order the browser fires
    them in.
    """
    if session.STATE["pending_kind"] != "question":
        return {"ok": False, "reason": "no question pending"}

    data = await request.json()
    answers = data.get("answers") or {}
    session.STATE["answers"] = answers
    return {"ok": True, "answers": answers}


@app.post("/api/chat/stop")
async def post_stop():
    session.STATE["stop"].set()
    return {"status": "stopping"}


@app.post("/api/chat/deny")
async def post_deny():
    """Resolve the pending approval as denied and drive the loop to its end.

    claude_loop answers a denial by yielding `cancelled` and returning, so this
    finishes in a couple of steps — no need to make the browser hold a stream
    open for it.
    """
    if session.STATE["pending"] is None:
        return {"ok": False, "reason": "nothing pending"}

    session.resolve_pending(False)
    gen = session.STATE["gen"]
    session.clear_pending()
    session.STATE["gen"] = None

    if gen is not None:
        try:
            async for _ in gen:
                pass
        except Exception:
            pass
        finally:
            await gen.aclose()

    store_msgs(_project_dir(), session.STATE["msgs"])
    return {"ok": True}


# ── static ─────────────────────────────────────────────────────────
# Mounted last so /api/* always wins.

if (DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")


@app.get("/")
async def index():
    idx = DIST / "index.html"
    if not idx.is_file():
        return JSONResponse(
            {"error": "GUI assets not built — run `npm run build` in frontend/"},
            status_code=503,
        )
    return FileResponse(idx)
