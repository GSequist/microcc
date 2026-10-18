"""FastAPI surface for the GUI: one project, one browser tab (cookie + tab id), the process is the session."""

import hmac
import os
import uuid
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile, File
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from micro_cc.claude_loop_ import claude_loop, GATEABLE_TOOLS
from micro_cc.models import catalog as model_catalog
from micro_cc.utils import command_registry, hidden_prompts, hints, settings_store_, whats_new
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
from micro_cc.webui import bridge, history, live, session

DIST = Path(__file__).parent / "dist"

UPLOAD_SUBDIR = "uploads"

# Palette from command_registry (shared with the TUI); drops gui=False commands.
COMMANDS = [
    {k: v for k, v in c.items() if k != "gui"}
    for c in command_registry.COMMANDS
    if c.get("gui", True)
]

# Typed-in prompts bypass the palette filter, so enforce gui=False in /api/chat too.
_GUI_ALLOWED_PROMPT_COMMANDS = {c["name"] for c in COMMANDS if c.get("kind") == "prompt"}

REWIND_LIMIT = 40

app = FastAPI(title="micro-cc")


def _cookie_name(request: Request) -> str:
    return f"mcc_{request.url.port}"  # cookies ignore ports; keep two GUIs apart


def _authed(request: Request) -> bool:
    cookie = session.STATE["cookie"]
    return cookie is None or hmac.compare_digest(request.cookies.get(_cookie_name(request), ""), cookie)


@app.middleware("http")
async def _gate(request: Request, call_next):
    """/api needs the session cookie; the first tab id seen owns the session."""
    if request.url.path.startswith("/api/") and session.STATE["cookie"] is not None:
        if not _authed(request):
            return JSONResponse({"error": "unauthorized"}, status_code=403)
        tab = request.headers.get("x-tab")
        if tab:
            owner = session.STATE["owner"] = session.STATE["owner"] or tab
            if tab != owner:
                return JSONResponse({"error": "session is open in another tab"}, status_code=409)
    return await call_next(request)


@app.get("/api/ping")
async def ping():
    return {"ok": True}


# ── helpers ────────────────────────────────────────────────────────

def _project_dir() -> str:
    return session.STATE["project_dir"]


def _safe_path(rel: str) -> Path | None:
    """Resolve `rel` inside project_dir, or None if it escapes (a ../ in rendered output must not read arbitrary files)."""
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
    # Required: without x-vercel-ai-ui-message-stream the SDK won't parse the stream.
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
    """Well-formed empty assistant turn, for a POST with nothing to do (e.g. an auto-send after a deny)."""
    yield bridge.sse({"type": "start", "messageId": f"msg-{uuid.uuid4().hex[:12]}"})
    yield bridge.sse({"type": "finish", "finishReason": "stop"})
    yield bridge.SSE_DONE


async def _refusal_stream(text: str):
    """Same shape as _empty_stream, with a visible reason for a command this surface can't run."""
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
            "model": settings.get("model", model_catalog.DEFAULT_MODEL),
            "models": model_catalog.MODEL_OPTIONS,
            "dangerous": settings.get("dangerous", []),
            "gateable": GATEABLE_TOOLS,
            "presets": [{"key": k, "label": p["label"], "description": p["description"]}
                        for k, p in settings_store_.GATE_PRESETS.items()],
            "preset": settings_store_.current_preset(),
            "tokens_budget": settings.get("tokens_budget", model_catalog.DEFAULT_TRIM_BUDGET),
        },
        "commands": COMMANDS,
        # Same registry the TUI hint bar rotates.
        "hints": hints.for_surface(hints.GUI),
        "whats_new": whats_new.pending(),
    }


@app.post("/api/whats-new/seen")
async def post_whats_new_seen():
    whats_new.mark_seen()
    return {"ok": True}


@app.get("/api/background-status")
async def get_background_status():
    return {"processes": live.bgprocs(_project_dir())}


@app.get("/api/subagents")
async def get_subagents():
    return {"subagents": live.subagents(_project_dir())}


@app.get("/api/incoming")
async def get_incoming(claim: bool = False):
    """Peek at the head of server-initiated turns; /api/chat consumes it, so a dropped send stays queued."""
    queue = session.STATE["incoming"]
    if claim and queue and session.STATE["pending"] is None and not session.STATE["lock"].locked():
        return {"id": queue[0]["id"], "content": queue[0]["content"], "queued": len(queue)}
    return {"id": None, "content": None, "queued": len(queue)}


@app.post("/api/settings")
async def post_settings(request: Request):
    """Write through to ~/.micro-cc/settings.json, the file the TUI pickers edit."""
    data = await request.json()
    if "model" in data:
        settings_store_.edit_setting("model", data["model"])
        session.STATE["model"] = data["model"]
    if "dangerous" in data:
        settings_store_.set_gated(data["dangerous"])
    if data.get("preset") in settings_store_.GATE_PRESETS:
        settings_store_.apply_preset(data["preset"])
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
    """Save attachments under {project_dir}/uploads/; the model reads them with its ordinary read_ tool."""
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

def _persist_partial(project_dir: str) -> None:
    """Save the user turn and any streamed-but-unsaved reply, like the TUI's cancel path."""
    msgs, partial = session.STATE["msgs"], "".join(session.STATE["partial"]).strip()
    if partial and msgs[-1:] and msgs[-1].get("role") != "assistant":
        msgs.append({"role": "assistant", "content": partial})
    session.STATE["partial"] = []
    store_msgs(project_dir, msgs)


@app.post("/api/chat")
async def post_chat(request: Request):
    data = await request.json()
    messages = data.get("messages", [])
    project_dir = _project_dir()
    incoming_id = data.get("incoming_id")

    query = _latest_user_text(messages)
    head, _, rest = query.strip().partition(" ")
    if head in hidden_prompts.PROMPT_BODIES:
        if head not in _GUI_ALLOWED_PROMPT_COMMANDS:
            return _sse_response(_refusal_stream(f"{head} is TUI-only — run it from the terminal instead."))
        query = hidden_prompts.build(head, rest.strip())  # same hidden turns the TUI pushes

    files = data.get("files") or []  # attachments are on disk; name them so the model looks
    if files:
        listed = "\n".join(f"- {f}" for f in files)
        query = f"Files attached to this message (relative to the project directory):\n{listed}\n\n{query}"

    async def run():
        # Everything touching session state happens under the lock: one turn at a time.
        async with session.STATE["lock"]:
            # Pending approval: claude_loop is parked on a yield; this POST resumes it, no new turn.
            resuming = session.STATE["pending"] is not None
            if not resuming and (
                not _ends_with_user_turn(messages)
                or (incoming_id is not None and session.take_incoming(incoming_id) is None)  # another tab took it
            ):
                async for chunk in _empty_stream():
                    yield chunk
                return

            session.STATE["stop"].clear()
            session.STATE["partial"] = []
            if resuming:
                # Approvals resume with a bool (False if /deny stashed one); questions with the answers dict.
                kind = session.STATE["pending_kind"]
                session.resolve_pending(session.STATE["answers"] if kind == "question" else not session.STATE["denied"])
                session.clear_pending()
                gen = session.STATE["gen"]
            else:
                msgs = session.STATE["msgs"] = load_msgs(project_dir)
                gen = session.STATE["gen"] = claude_loop(
                    query=query,
                    msgs=msgs,
                    project_dir=project_dir,
                    model=settings_store_.get_setting("model") or model_catalog.DEFAULT_MODEL,
                    max_tokens=session.STATE["trim_budget"],
                    surface="gui",
                )

            try:
                async for chunk in bridge.stream(gen, f"msg-{uuid.uuid4().hex[:12]}", project_dir):
                    yield chunk
                    if session.STATE["stop"].is_set():
                        break
            finally:
                if session.STATE["pending"] is None:  # keep the generator alive while parked on an approval
                    _persist_partial(project_dir)  # sync: durable even if the awaits below are cancelled
                    session.STATE["gen"] = None
                    try:
                        await gen.aclose()
                    finally:
                        store_msgs(project_dir, session.STATE["msgs"])

    return _sse_response(run())


@app.post("/api/chat/answer")
async def post_answer(request: Request):
    """Stash question answers; the resume re-POST to /api/chat comes separately, so order doesn't matter."""
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
    """Stash a deny; the resume re-POST to /api/chat hands it to the parked loop, which keeps going."""
    if session.STATE["pending_kind"] != "approval":
        return {"ok": False, "reason": "no approval pending"}
    session.STATE["denied"] = True
    return {"ok": True}


# ── static ─────────────────────────────────────────────────────────
# Mounted last so /api/* always wins.

if (DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")


@app.get("/")
async def index(request: Request, t: str = ""):
    """The /gui link carries a one-time token; swap it for the cookie and drop it from the URL."""
    token = session.STATE["token"]
    if token and hmac.compare_digest(t, token):
        session.STATE["token"] = None
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(_cookie_name(request), session.STATE["cookie"], httponly=True, samesite="strict")
        return resp
    if not _authed(request):
        return HTMLResponse("This link has expired. Run /gui in the terminal for a new one.", status_code=403)
    idx = DIST / "index.html"
    if not idx.is_file():
        return JSONResponse(
            {"error": "GUI assets not built — run `npm run build` in frontend/"},
            status_code=503,
        )
    return FileResponse(idx)
