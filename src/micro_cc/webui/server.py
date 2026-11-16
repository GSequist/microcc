"""FastAPI surface for the GUI: one project, one browser tab (cookie + tab id), the process is the session."""

import asyncio
import contextlib
import hmac
import json
import os
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile, File
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from micro_cc.claude_loop_ import claude_loop, GATEABLE_TOOLS
from micro_cc.models import catalog as model_catalog
from micro_cc.models.providers import active_provider
from micro_cc.utils import command_registry, hidden_prompts, hints, keep_awake_, settings_store_, whats_new
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

# > hidden-tab timer throttling (Chrome pings at most once a minute), so a backgrounded owner keeps its tab.
OWNER_LEASE_S = 75
COOKIE_MAX_AGE_S = 30 * 24 * 3600  # survives a browser restart; a new /gui server mints a new one anyway

app = FastAPI(title="micro-cc")


def _cookie_name(request: Request) -> str:
    return f"mcc_{request.url.port}"  # cookies ignore ports; keep two GUIs apart


def _authed(request: Request) -> bool:
    cookie = session.STATE["cookie"]
    return cookie is None or hmac.compare_digest(request.cookies.get(_cookie_name(request), ""), cookie)


@app.middleware("http")
async def _gate(request: Request, call_next):
    """/api needs the session cookie; one tab owns the session, on a lease a silent tab loses."""
    if request.url.path.startswith("/api/") and session.STATE["cookie"] is not None:
        if not _authed(request):
            return JSONResponse({"error": "unauthorized"}, status_code=403)
        now = time.monotonic()
        tab = request.headers.get("x-tab")
        if tab:
            st = session.STATE
            if st["owner"] not in (None, tab) and now - st["owner_seen"] < OWNER_LEASE_S:
                return JSONResponse({"error": "session is open in another tab"}, status_code=409)
            st["owner"], st["owner_seen"] = tab, now
        session.STATE["last_seen"] = now
    return await call_next(request)


@app.get("/api/ping")
async def ping():
    return {"ok": True}


@app.post("/api/release")
async def release(tab: str = ""):
    """sendBeacon on pagehide: a closed or reloading tab frees the session at once instead of after the lease."""
    if tab and session.STATE["owner"] == tab:
        session.STATE["owner"] = None
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


async def _refusal_stream(text: str, resync: bool = False):
    """Same shape as _empty_stream, with a visible reason for a command this surface can't run.
    resync: the tab's view is stale (e.g. typed over a parked approval); it reloads history after."""
    yield bridge.sse({"type": "start", "messageId": f"msg-{uuid.uuid4().hex[:12]}"})
    if resync:
        yield bridge.sse({"type": "data-resync", "data": {}, "transient": True})
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
    if claim and queue and not session.busy():
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
    """Disk history; mid-turn, history up to the turn's user message, and `live` tells the tab to replay the rest."""
    turn = session.STATE["turn"]
    if turn is not None and not turn["done"]:
        return {"messages": turn["ui_base"], "live": True}
    return {"messages": history.to_ui_messages(load_msgs(_project_dir())), "live": False}


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
    if session.busy():
        return JSONResponse({"error": "a turn is running or waiting on you — finish or stop it first"}, status_code=423)
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

    if name in ("/clear", "/compact") and session.busy():  # would rewrite history under a live or parked loop
        return {"message": f"{name} — a turn is running or waiting on you; finish or stop it first"}

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

    if name in ("/mcp-login", "/mcp-logout"):
        return {"message": await _mcp_command(name, (data.get("args") or "").strip(), project_dir)}

    return {"message": f"{name} is not available here"}


async def _mcp_command(name: str, arg: str, project_dir: str) -> str:
    """No name: list OAuth servers. Login runs in the background on this loop; the reply carries the
    auth URL notice (first ~20s), and the outcome shows on the next bare /mcp-login."""
    from micro_cc.tools import mcp_login_
    if not arg:
        rows = await mcp_login_.oauth_servers(project_dir)
        return mcp_login_.format_list(rows) + (f"\n\nRun {name} <name>." if rows else "")
    if name == "/mcp-logout":
        return await mcp_login_.logout(project_dir, arg)
    notes: list[str] = []
    noticed = asyncio.Event()

    def notify(text: str) -> None:
        notes.append(text)
        noticed.set()

    started = await mcp_login_.start_login(project_dir, arg, notify)
    if started.startswith("starting"):
        try:
            await asyncio.wait_for(noticed.wait(), 20)
        except TimeoutError:
            pass
    return "\n".join([started, *notes])


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
        # One turn at a time: a second POST waits for the running one (no await between check and claim below).
        while (t := session.STATE["turn"]) is not None and t["running"]:
            await t["wake"].wait()

        st = session.STATE
        notice = None
        if st["pending"] is not None:
            # Parked claude_loop resumes only on this tab's explicit answer to *that* call — a plain new
            # message (e.g. typed after a reload) must never count as an approval.
            tool_id = (st["pending_tool"] or {}).get("id")
            approved = _approval_response(messages, tool_id)
            turn = st["turn"]
            if approved is None or turn is None:
                async for chunk in _refusal_stream("Approve or deny the pending action above first.", resync=True):
                    yield chunk
                return
            # Approvals resume with a bool (False if /deny stashed one); questions with the answers dict.
            approved = approved and not st["denied"]
            session.resolve_pending((st["answers"] if approved else None) if st["pending_kind"] == "question" else approved)
            session.clear_pending()
            gen = st["gen"]
        elif not _ends_with_user_turn(messages) or (
            incoming_id is not None and session.take_incoming(incoming_id) is None  # another tab took it
        ):
            async for chunk in _empty_stream():
                yield chunk
            return
        else:
            msgs = st["msgs"] = load_msgs(project_dir)
            saved = settings_store_.get_setting("model") or model_catalog.DEFAULT_MODEL
            model, replaced = model_catalog.usable_model(saved, active_provider())
            notice = model_catalog.stale_model_notice(saved) if replaced else None
            turn = st["turn"] = {
                "msg_id": f"msg-{uuid.uuid4().hex[:12]}",  # one id across approval segments: the tab extends one message
                "ui_base": history.to_ui_messages(msgs) + messages[-1:],
                "segments": [],
                "running": False,
                "done": False,
                "wake": asyncio.Event(),
            }
            gen = st["gen"] = claude_loop(
                query=query,
                msgs=msgs,
                project_dir=project_dir,
                model=model,
                max_tokens=st["trim_budget"],
                surface="gui",
            )

        st["stop"].clear()
        st["partial"] = []
        turn["segments"].append([])
        turn["running"] = True
        # The turn is a task, not this response: a dropped tab (reload, sleep, discard) no longer kills it.
        turn["task"] = asyncio.create_task(_drive(turn, gen, notice, project_dir))
        async for chunk in _follow(turn, replay=False):
            yield chunk

    return _sse_response(run())


def _approval_response(messages: list, tool_id: str | None) -> bool | None:
    """The tab's approve/deny for `tool_id` (SDK state approval-responded), or None if it sent none."""
    for msg in reversed(messages):
        for part in msg.get("parts") or []:
            if tool_id and part.get("toolCallId") == tool_id and part.get("state") == "approval-responded":
                return bool((part.get("approval") or {}).get("approved"))
    return None


# Completed (pre-approval) segments replay as plain history: no re-armed approval, no re-asked question.
_REPLAY_SKIP = {"finish", "done", "tool-approval-request", "data-question"}


def _wake(turn: dict) -> None:
    ev, turn["wake"] = turn["wake"], asyncio.Event()
    ev.set()


def _emit(turn: dict, chunk: str) -> None:
    kind = "done" if chunk == bridge.SSE_DONE else json.loads(chunk[6:]).get("type")
    turn["segments"][-1].append((kind, chunk))
    _wake(turn)


async def _drive(turn: dict, gen, notice: str | None, project_dir: str) -> None:
    """Run one segment of the turn into its buffer; ends on finish, a park, /api/chat/stop or shutdown."""
    st = session.STATE
    keep_awake_.hold("gui-turn")  # the TUI holds "turn" for its own turns; a GUI turn must not sleep either
    stream = bridge.stream(gen, turn["msg_id"], project_dir, notice)
    try:
        async with st["lock"]:
            try:
                async with contextlib.aclosing(stream):
                    async for chunk in stream:
                        _emit(turn, chunk)
                        if st["stop"].is_set():
                            break
            except Exception as e:
                _emit(turn, bridge.sse({"type": "error", "errorText": f"{type(e).__name__}: {e}"}))
            finally:
                if st["pending"] is None:  # keep the generator alive while parked on an approval
                    _persist_partial(project_dir)  # sync: durable even if the awaits below are cancelled
                    st["gen"] = None
                    try:
                        await gen.aclose()
                    finally:
                        store_msgs(project_dir, st["msgs"])
    finally:
        keep_awake_.hold("gui-turn", False)
        seg = turn["segments"][-1]
        if not seg or seg[-1][0] != "done":  # stopped/failed mid-stream: still close it for followers
            _emit(turn, bridge.sse({"type": "finish", "finishReason": "stop"}))
            _emit(turn, bridge.SSE_DONE)
        turn["done"] = st["pending"] is None
        turn["running"] = False
        _wake(turn)


async def _follow(turn: dict, replay: bool):
    """Yield the turn's SSE: the current segment live, or (replay) everything since the turn began."""
    segs = turn["segments"]
    last = len(segs) - 1
    if replay:
        for si in range(last):
            for kind, chunk in segs[si]:
                if kind not in _REPLAY_SKIP and not (kind == "start" and si > 0):
                    yield chunk
    seg, i = segs[last], 0
    while True:
        while i < len(seg):
            kind, chunk = seg[i]
            i += 1
            if not (replay and kind == "start" and last > 0):
                yield chunk
            if kind == "done":
                return
        if not turn["running"]:
            return
        await turn["wake"].wait()


@app.get("/api/chat/{chat_id}/stream")
async def resume_stream(chat_id: str):
    """AI SDK resumeStream(): replay the live turn into a reconnected tab; 204 when there is none."""
    turn = session.STATE["turn"]
    if turn is None or turn["done"]:
        return Response(status_code=204)
    return _sse_response(_follow(turn, replay=True))


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
        resp.set_cookie(_cookie_name(request), session.STATE["cookie"], httponly=True, samesite="strict",
                        max_age=COOKIE_MAX_AGE_S)
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
