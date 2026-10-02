import asyncio
import datetime
import os
from functools import partial

from dotenv import load_dotenv
from jsonschema import validators
from jsonschema.exceptions import best_match

from micro_cc.execute_tool import execute_tool_call
from micro_cc.models import model_call
from micro_cc.models.registry import DEFAULT_MODEL, DEFAULT_TRIM_BUDGET
from micro_cc.utils.tool_limits import cap_for, MAX_TOOL_RESULTS_PER_MESSAGE
from micro_cc.utils.tool_result_storage import persist_and_preview
from micro_cc.models.schema import function_to_schema
from micro_cc.skills.skill_loader import get_skill_summary
from micro_cc.tools.bash_tool import bash_
from micro_cc.tools.monitor_ import monitor_
from micro_cc.tools.file_tools_ import read_, write_, edit_, glob_, grep_
from micro_cc.tools.search_tool_ import search_tools, list_mcps
from micro_cc.tools.use_tool_ import use_tool_, resolve_call, check_wrapper_shape, inner_block, DISPATCHER
from micro_cc.tools.todo_tools_ import todo_tool_
from micro_cc.tools.memory_tool_ import memory_
from micro_cc.tools.search_history_tool_ import search_history_
from micro_cc.utils import memory_store_
from micro_cc.tools.skill_tools_ import read_skill, list_skills
from micro_cc.tools.ask_user_tool import ask_user_question_tool_, validate_ask_questions
from micro_cc.tools.search_tool_ import (
    get_tool_func,
    get_effective_mcp_catalog,
    find_tool_schema,
    parse_names,
    schema_text,
    MCP_RESOLVED,
    TOOL_CATALOG,
)
from micro_cc.tools.mcp_client_ import resolve_mcp_tools, call_mcp_tool
from micro_cc.utils.msg_store_ import load_summary, load_checkpoint, compact_checkpoint, store_msgs
from micro_cc.utils.tokenization_simple import token_cutter, token_stats, save_token_stats, detect_cache_miss, record_prompt_segments
from micro_cc.utils.claude_md_loader import load_claude_md_file
from micro_cc.utils.helpers import get_endpoint


load_dotenv(os.path.expanduser("~/.micro-cc/.env"))
load_dotenv()

DEFAULT_DANGEROUS = {"bash_", "edit_", "write_"}
GATEABLE_TOOLS = [
    "bash_", "write_", "edit_", "read_", "glob_", "grep_",
    "computer_use", "browser_navigate",
]
ASK_USER_QUESTION_TOOL = ["ask_user_question_tool_"]

def _unanswered_results(blocks, calls, results, started):
    """Answer every call of a cut-short turn: real results kept, the rest marked not run or interrupted."""
    out = []
    for tb in blocks:
        name = calls[tb.id][0]
        if tb.id in results:
            r = results[tb.id]
            text = str(r.get("output", "")) if isinstance(r, dict) and r.get("type") != "image" else str(r or "")
            out.append({"type": "tool_result", "tool_use_id": tb.id, "content": text[:cap_for(name)]})
            continue
        why = (
            f"Interrupted: the turn was cancelled while {name} was running. It may have partly run; check the state before retrying."
            if tb.id in started
            else f"Not run: the turn was cancelled or failed before {name} started."
        )
        out.append({"type": "tool_result", "tool_use_id": tb.id, "content": why, "is_error": True})
    return out


# Write tools must serialize; reads are safe in parallel. Unlisted names count as unsafe.
_CONCURRENCY_SAFE_TOOLS = {
    "read_", "glob_", "grep_",
    "read_skill", "list_skills", "list_mcps",
}


def _is_concurrency_safe(name: str, tool_input: dict) -> bool:
    if name in _CONCURRENCY_SAFE_TOOLS:
        return True
    if name == "memory_":
        return tool_input.get("action") in ("get", "list")
    if name == "search_tools":
        return tool_input.get("action") == "discover"
    return False


class _SpinGuard:
    """Detects thinking stall via repeated low-content lines in sliding window."""

    WINDOW = 40         # lines tracked in the sliding window
    MIN_LINES = 30      # don't judge until this many lines have gone by
    MAX_DISTINCT = 6    # ...and this few distinct among the last WINDOW

    def __init__(self):
        from collections import deque
        self._tail = ""
        self._window = deque(maxlen=self.WINDOW)
        self.lines = 0
        self.tripped = False

    @staticmethod
    def _split(text: str):
        """Cut at first newline; return (line, rest) or (None, text)."""
        i = text.find("\n")
        if i == -1:
            return None, text
        return text[:i], text[i + 1:]

    def feed(self, chunk: str) -> bool:
        """Return True if stall detected; caller should stop stream."""
        self._tail += chunk
        while True:
            line, self._tail = self._split(self._tail)
            if line is None:
                break
            line = line.strip()
            if not line:
                continue
            self.lines += 1
            self._window.append(line)
            if (self.lines >= self.MIN_LINES
                    and len(self._window) >= self.WINDOW
                    and len(set(self._window)) <= self.MAX_DISTINCT):
                self.tripped = True
                return True
        if len(self._tail) > 1_000_000:
            self._tail = ""   # pathological single line — never the pattern
        return False


# Injected when _SpinGuard trips
_SPIN_NUDGE = (
    "<system-reminder>\nYour thinking stalled: it degenerated into a run of "
    "very short lines (\"Go.\", \"Now.\", \"Let me write.\") with no tool call, "
    "so the turn was cut. Do not re-plan in thinking. Pick the single next "
    "concrete action and emit its tool call now. If the task is genuinely "
    "multi-step, record it with todo_tool_ instead of holding it in thinking.\n"
    "</system-reminder>"
)


def _partition_tool_batches(ordered_blocks):
    """Group tool calls: safe ones parallel, unsafe ones serial."""
    batches = []
    for tb, is_mcp in ordered_blocks:
        name, args = resolve_call(tb)
        safe = False if is_mcp else _is_concurrency_safe(name, args)
        if safe and batches and batches[-1][0]:
            batches[-1][1].append((tb, is_mcp))
        else:
            batches.append((safe, [(tb, is_mcp)]))
    return batches

def _loaded_names(msgs) -> set:
    """Extract tool/MCP names from search_tools add calls in history."""
    loaded = set()
    for msg in msgs:
        content = msg.get("content")
        if msg.get("role") != "assistant" or not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name, args = resolve_call(block)
                if name == "search_tools" and args.get("action") == "add":
                    loaded.update(parse_names(args.get("names")))
    return loaded


def _arg_error(schema: dict, args: dict) -> str | None:
    """Validate args against schema; return first error or None."""
    input_schema = schema.get("input_schema") or {}
    try:
        cls = validators.validator_for(input_schema)
        cls.check_schema(input_schema)
    except Exception:
        return None
    err = best_match(cls(input_schema).iter_errors(args))
    if err is None:
        return None
    where = "/".join(str(p) for p in err.absolute_path)
    return err.message + (f" (at {where})" if where else "")


async def claude_loop(
    query,
    msgs,
    *,
    project_dir,
    model=DEFAULT_MODEL,
    max_tokens=DEFAULT_TRIM_BUDGET,
    dangerous_tools=None,
    surface="headless",
    encoded_image=None,
):
    """Main agent loop: calls model, executes tools, appends results to msgs."""

    dangerous = DEFAULT_DANGEROUS if dangerous_tools is None else set(dangerous_tools)
    ask_user = ASK_USER_QUESTION_TOOL
    end_resp = get_endpoint()

    skills_summary = get_skill_summary(project_dir)
    claude_md_content = load_claude_md_file(project_dir)
    mcp_catalog = get_effective_mcp_catalog(project_dir)
    # micro_cc's own source dir (not project_dir): this file sits directly in the package dir.
    package_dir = os.path.dirname(os.path.abspath(__file__))

    default_tools = [
        bash_,
        read_, write_, edit_, glob_, grep_,
        search_tools,
        use_tool_,
        todo_tool_,
        memory_,
        search_history_,
        read_skill, list_skills, list_mcps,
        ask_user_question_tool_,
        monitor_,
    ]
    tool_schemas = []
    for tool in default_tools:
        tool_schemas.append(function_to_schema(tool))

    tools = {}
    for tool in default_tools:
        if isinstance(tool, partial):
            tools[tool.func.__name__] = tool
        else:
            tools[tool.__name__] = tool

    core_schemas = {schema["name"]: schema for schema in tool_schemas}
    mcp_routing = {}
    resolved_mcp_names = set()

    def _register_mcp(server_name, routing):
        mcp_routing.update(routing)
        resolved_mcp_names.add(server_name)

    yield {"type": "status", "message": "", "msgs": msgs}

    # Built per call, never appended to msgs: a stored copy would replay a stale date/CLAUDE.md on resume.
    claude_md_section = f"\n\n<project-instructions>\n{claude_md_content}\n</project-instructions>" if claude_md_content else ""

    orchestration_line = (
        "- Multi-subagent orchestration: read_skill('graph-orchestration') to "
        "self-initiate a /graph-style orchestration any time you judge a task "
        "benefits from parallel or sequenced headless subagents — the user "
        "doesn't have to type /graph first."
    ) if surface == "tui" else ""

    system_prompt_msg = {
        "role": "system",
        "content": f"""You are micro cognitive compute - a CLI assistant. You have full system access and thus using code you can achieve any task.

## Environment
- You are running LOCALLY on the user's machine (on the metal), NOT a remote server
- You have direct access to the local filesystem, Desktop, Documents, etc.
- Project directory: {project_dir}
- Your own source code lives at: {package_dir} (not project_dir) — plain, editable .py. You can change the harness itself: edit it and the running process restarts into your change (auto at the next idle turn, or /reload to force it now); the conversation persists across the restart.
- Date: {datetime.datetime.now().strftime("%B %d, %Y")}

## Core Tools (always available)
- bash_: Execute shell commands (runs in project_dir, use path param or absolute paths for elsewhere)
- read_: Read file contents with line numbers — also reads image files (png/jpg/gif/webp/bmp) directly, no separate tool needed
- write_: Create/overwrite files
- edit_: Surgical string replacement in files
- glob_: Find files by pattern
- grep_: Search file contents with regex

## Discoverable Tools
search_tools is two-step and cheap: `search_tools(action="discover")` lists
every tool and MCP you could load (name + one-line description, no schemas),
then `search_tools(action="add", names="browser, deepwiki")` returns the
schemas of the ones you want — including several at once — as text in its
result. Those tools are not in your tool list: call each one through the
fixed dispatcher `use_tool_(name="browser", args=...)` with args matching the
schema you were given. A call with args that don't match the schema fails
and the error carries the schema, so retry from that.
Do this when you need a capability you don't currently have: web research,
browser, computer_use, message_session_ (list and message other live
micro-cc sessions), etc.
{orchestration_line}

## Planning Tools
- todo_tool_: For multi-step complex work, write a todo plan and keep on track.

## Long-term Memory
memory_ (action=add|edit|get|delete|list, scope=global|project).
scope="global": cross-project facts (~/.micro-cc/memory.json). scope="project":
this project only (~/.micro-cc/projects/{{name}}_{{hash}}/memory.json).
<memory-manifest> lists keys each loop; memory_(action="get", key=...) expands
one. In-progress state: use todo_tool_ instead.

## Conversation History Search
conversation grows forever
search_history_(query=..., limit=...): full-text search over this project's
whole conversation history, including compacted turns.

{skills_summary}
{claude_md_section}

## Guidelines
- Read files before editing them
- Use absolute paths when working outside project_dir
- When writing to user, remove all mannered prose
""",
        # cache breakpoint: tools + this prompt, byte-identical across iterations of this call
        "cache_control": {"type": "ephemeral"},
    }

    images = [encoded_image] if isinstance(encoded_image, str) else (encoded_image or [])
    if images:
        note = "image" if len(images) == 1 else f"{len(images)} images"
        content = [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": img},
            }
            for img in images
        ] + [
            {"type": "text", "text": f"\n[{note.capitalize()} attached above — you can see {'it' if len(images) == 1 else 'them'} directly, no need to read_ {'it' if len(images) == 1 else 'them'} again]\n\n{query}\n"},
        ]
    else:
        content = f"""
{query}\n
        """
    msgs.append({"role": "user", "content": content})

    def _build_memory_manifest() -> str | None:
        """Index memory keys across global and project scopes."""
        parts = []
        global_items = memory_store_.list_memories()
        if global_items:
            parts.append(
                "Global (all projects):\n"
                + "\n".join(f"- {it['key']} — {it['description']}" for it in global_items)
            )
        project_items = memory_store_.list_memories(project_dir=project_dir)
        if project_items:
            parts.append(
                "Project (this project_dir only):\n"
                + "\n".join(f"- {it['key']} — {it['description']}" for it in project_items)
            )
        return "\n\n".join(parts) if parts else None

    _memory_manifest_baseline = _build_memory_manifest()
    # Own system block with its own cache breakpoint; frozen for the turn so memory edits don't miss the prompt cache.
    memory_manifest_msg = (
        {
            "role": "system",
            "content": f"## Long-term Memory\nKey+description index for both scopes — call memory_(action=\"get\", key=..., scope=...) to expand an entry.\n\n{_memory_manifest_baseline}",
            "cache_control": {"type": "ephemeral"},
        }
        if _memory_manifest_baseline is not None
        else None
    )
    _last_shown_manifest = _memory_manifest_baseline

    while True:
        checkpoint = load_checkpoint(project_dir)
        # /rewind can leave msgs shorter than as_of_index: treat that as no checkpoint.
        if checkpoint and checkpoint["as_of_index"] > len(msgs):
            checkpoint = None

        # Fire-and-forget: this turn still truncates via token_cutter, a new checkpoint applies next turn.
        asyncio.create_task(compact_checkpoint(project_dir, msgs, len(msgs), model))

        # Volatile status rides in ONE trailing _ephemeral user message, after every cache anchor.
        status_sections = []

        from micro_cc.tools.file_tools_ import format_external_changes
        file_changes = format_external_changes()
        if file_changes:
            status_sections.append(f"<file-changes>\n{file_changes}\n</file-changes>")

        from micro_cc.tools.bash_tool import format_background_status
        proc_info = format_background_status()
        if proc_info:
            status_sections.append(f"<process-status>\n{proc_info}\n</process-status>")

        # Old-format summary only; with a checkpoint its text already rides inside trimmed_rest.
        if not checkpoint:
            conversation_summary = load_summary(project_dir)
            if conversation_summary:
                status_sections.append(f"<conversation-summary>\n{conversation_summary}\n</conversation-summary>")

        # Mid-turn memory_ edits surface as a tail delta; the frozen manifest block is not rebuilt.
        current_manifest = _build_memory_manifest()
        if current_manifest is not None and current_manifest != _last_shown_manifest:
            status_sections.append(
                "<memory-manifest-update>\nMemory changed since this turn started "
                f"(still call memory_(action=\"get\", ...) to expand an entry):\n\n{current_manifest}\n</memory-manifest-update>"
            )
            _last_shown_manifest = current_manifest

        status_msgs = []
        if status_sections:
            status_msgs.append({
                "role": "user",
                "content": "<system-reminder>\n" + "\n\n".join(status_sections) + "\n</system-reminder>",
                "_ephemeral": True,
            })

        if checkpoint:
            trimmed_rest = token_cutter(
                msgs, max_tokens,
                start_index=checkpoint["as_of_index"],
                checkpoint_summary=checkpoint["content"],
                folded_tokens=checkpoint["folded_tokens"],
            )
        else:
            trimmed_rest = token_cutter(msgs, max_tokens)
        # Cache order: system, memory manifest, transcript, ephemeral status last.
        trimmed_loop_msgs = (
            [system_prompt_msg]
            + ([memory_manifest_msg] if memory_manifest_msg else [])
            + trimmed_rest
            + status_msgs
        )

        record_prompt_segments(
            tool_schemas,
            [system_prompt_msg] + ([memory_manifest_msg] if memory_manifest_msg else []),
        )

        pending = None
        try:
            stream = await model_call(
                input=trimmed_loop_msgs,
                model=model,
                endpoint=end_resp,
                tools=tool_schemas,
                thinking=True,
                stream=True,
            )

            if stream is None:
                yield {"type": "error", "message": "model call failed after retries"}
                break

            response = None
            api_usage = {}
            spin_guard = _SpinGuard()
            spin_tripped = False
            async for event in stream:
                if event["type"] == "text_delta":
                    yield {"type": "text_delta", "content": event["text"]}
                elif event["type"] == "thinking_delta":
                    chunk = event["thinking"]
                    if spin_guard.feed(chunk):
                        spin_tripped = True
                        break
                    yield {"type": "thinking_delta", "content": chunk}
                elif event["type"] == "response":
                    response = event["response"]
                    api_usage = event.get("usage", {})

            # Drop the partial turn (nothing was persisted); the nudge is _ephemeral so it stays out of the cache anchor.
            if spin_tripped:
                msgs.append({"role": "user", "content": _SPIN_NUDGE, "_ephemeral": True})
                yield {"type": "error", "message": "thinking stalled (short-line loop) — turn cut and model re-prompted"}
                continue

            # Call detect_cache_miss before token_stats['input'] is overwritten below.
            cache_miss = None
            if api_usage.get("input"):
                cache_miss = detect_cache_miss(api_usage, model)
                token_stats["input"] = api_usage["input"]
                token_stats["total_input"] += api_usage["input"]
            if api_usage.get("output"):
                token_stats["output"] += api_usage["output"]
            if api_usage:
                save_token_stats(project_dir)
            if cache_miss:
                yield {"type": "cache_invalidate", **cache_miss}

            if response is None:
                yield {"type": "error", "message": "model call failed after retries"}
                break

            thinking_block = next(
                (block for block in response.content if block.type == "thinking"), None
            )
            text_block = next(
                (block for block in response.content if block.type == "text"), None
            )
            tool_use_blocks = [
                block for block in response.content if block.type == "tool_use"
            ]

            if tool_use_blocks:
                # Streamed args that failed to parse never execute: they get an is_error result.
                malformed_by_id = {tb.id: tb.parse_error for tb in tool_use_blocks if tb.parse_error}

                # Everything below keys on the INNER call; the transcript keeps the literal use_tool_ block.
                calls = {tb.id: resolve_call(tb) for tb in tool_use_blocks}

                content_blocks = []
                if thinking_block:
                    content_blocks.append(
                        {
                            "type": "thinking",
                            "thinking": thinking_block.thinking,
                            "signature": thinking_block.signature,
                            # OpenAI Responses can't replay reasoning without the block id.
                            "id": thinking_block.id,
                        }
                    )
                if text_block:
                    content_blocks.append({"type": "text", "text": text_block.text})
                for tb in tool_use_blocks:
                    content_blocks.append(
                        {"type": "tool_use", "id": tb.id, "name": tb.name, "input": tb.input}
                    )
                # Write-ahead: the call is on disk before any tool runs; the finally below always answers it.
                msgs.append({"role": "assistant", "content": content_blocks})
                store_msgs(project_dir, msgs)
                started, results_by_id = set(), {}
                pending = (tool_use_blocks, calls, results_by_id, started)
                visible_loaded = _loaded_names(trimmed_loop_msgs)

                call_errors_by_id = {}
                for tb in tool_use_blocks:
                    if tb.id in malformed_by_id:
                        continue
                    inner, inner_args = calls[tb.id]
                    reason = check_wrapper_shape(tb)
                    if reason:
                        call_errors_by_id[tb.id] = f"Error: {reason}"
                        continue

                    # Register on demand: a catalog function, or an MCP tool of a
                    # server this context loaded but this process hasn't resolved
                    # yet (fresh process / restart — mcp_routing starts empty).
                    if inner not in tools and inner not in mcp_routing:
                        if inner in TOOL_CATALOG:
                            tools[inner] = get_tool_func(inner)
                        else:
                            for server in visible_loaded:
                                if server in mcp_catalog and server not in resolved_mcp_names:
                                    mcp_tools, routing, failures = await resolve_mcp_tools([mcp_catalog[server]])
                                    for failure in failures:
                                        yield {"type": "error", "message": failure}
                                    if not failures:
                                        MCP_RESOLVED[server] = {"tools": mcp_tools, "routing": routing}
                                        _register_mcp(server, routing)
                                if inner in mcp_routing:
                                    break

                    if tb.name != DISPATCHER:
                        continue  # direct call: no schema contract to enforce
                    schema = core_schemas.get(inner) or find_tool_schema(inner)
                    if schema is None:
                        continue  # unknown tool: reported below
                    invalid = _arg_error(schema, inner_args)
                    if invalid:
                        call_errors_by_id[tb.id] = (
                            f"Error: invalid args for '{inner}': {invalid}. "
                            f"Schema:\n{schema_text(schema)}"
                        )

                # Split into: local tools, client-side MCP tools, truly unknown
                skip_ids = set(malformed_by_id) | set(call_errors_by_id)
                local_tool_blocks = [tb for tb in tool_use_blocks
                                     if calls[tb.id][0] in tools and tb.id not in skip_ids]
                mcp_tool_blocks = [tb for tb in tool_use_blocks
                                   if calls[tb.id][0] in mcp_routing and tb.id not in skip_ids]
                unknown_blocks = [tb for tb in tool_use_blocks
                                  if calls[tb.id][0] not in tools and calls[tb.id][0] not in mcp_routing
                                  and tb.id not in skip_ids]

                # Unknown tool names get an error result inside the single turn message, never a separate
                # assistant message (thinking/text would be echoed twice).
                unknown_ids = {tb.id for tb in unknown_blocks}

                # Malformed ask_user questions never reach the UI (it indexes them without fallbacks): error result instead.
                ask_errors_by_id = {}
                for tb in local_tool_blocks:
                    if calls[tb.id][0] in ask_user:
                        reason = validate_ask_questions(calls[tb.id][1].get("questions"))
                        if reason:
                            ask_errors_by_id[tb.id] = reason

                answered_by_id = {}
                for tool_use_block in local_tool_blocks:
                    inner_name, inner_args = calls[tool_use_block.id]
                    yield {
                        "type": "tool_call",
                        "name": inner_name,
                        "input": inner_args,
                        "id": tool_use_block.id,
                    }

                    if inner_name in ask_user and tool_use_block.id not in ask_errors_by_id:
                        answered = {"answered": None}
                        answered_by_id[tool_use_block.id] = answered
                        yield {
                            "type": "question_asked",
                            "id": tool_use_block.id,
                            "name": inner_name,
                            "input": inner_args,
                            "answered": answered,
                        }
                        if not answered["answered"]:
                            yield {"type": "cancelled"}
                            return

                    if inner_name in dangerous:
                        approval = {"approved": None}
                        yield {
                            "type": "approval_request",
                            "id": tool_use_block.id,
                            "name": inner_name,
                            "input": inner_args,
                            "approval": approval,
                        }
                        if not approval["approved"]:
                            yield {"type": "cancelled"}
                            return

                executed_local_blocks = [
                    tb for tb in local_tool_blocks if calls[tb.id][0] not in ask_user
                ]
                executed_ids = {tb.id for tb in executed_local_blocks} | {tb.id for tb in mcp_tool_blocks}

                # Restore emission order: _partition_tool_batches needs it to group reads correctly.
                ordered_blocks = [
                    (tb, calls[tb.id][0] in mcp_routing)
                    for tb in tool_use_blocks
                    if tb.id in executed_ids
                ]

                def _run(tb, is_mcp):
                    inner_name, inner_args = calls[tb.id]
                    if is_mcp:
                        return call_mcp_tool(
                            mcp_routing[inner_name]["url"],
                            inner_name,
                            inner_args,
                            headers=mcp_routing[inner_name].get("headers"),
                        )
                    return execute_tool_call(inner_block(tb), tools, project_dir, model)

                async def _run_and_record(tb, is_mcp):
                    started.add(tb.id)
                    try:
                        results_by_id[tb.id] = await _run(tb, is_mcp)
                    except Exception as e:
                        results_by_id[tb.id] = f"Tool error ({calls[tb.id][0]}): {type(e).__name__}: {e}"

                # Reads in a row run in parallel; a mutating tool runs alone.
                for is_safe, batch in _partition_tool_batches(ordered_blocks):
                    await asyncio.gather(*(_run_and_record(tb, is_mcp) for tb, is_mcp in batch))

                for tid, answered in answered_by_id.items():
                    results_by_id[tid] = answered["answered"]

                tool_result_blocks = []
                persisted_this_turn = set()
                for tool_block in tool_use_blocks:
                    if tool_block.id in malformed_by_id:
                        tool_result_blocks.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_block.id,
                                "content": (
                                    f"Error: arguments for tool '{tool_block.name}' were not valid JSON "
                                    f"({malformed_by_id[tool_block.id]}). Retry the call with valid JSON input."
                                ),
                                "is_error": True,
                            }
                        )
                        continue

                    if tool_block.id in call_errors_by_id:
                        tool_result_blocks.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_block.id,
                                "content": call_errors_by_id[tool_block.id],
                                "is_error": True,
                            }
                        )
                        continue

                    inner_name, inner_args = calls[tool_block.id]
                    if tool_block.id in unknown_ids:
                        tool_result_blocks.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_block.id,
                                "content": (
                                    f"Error: tool '{inner_name}' does not exist. Use search_tools "
                                    "to discover available tools, add it, then call it via use_tool_."
                                ),
                                "is_error": True,
                            }
                        )
                        continue

                    if tool_block.id in ask_errors_by_id:
                        tool_result_blocks.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_block.id,
                                "content": f"Error: invalid input — {ask_errors_by_id[tool_block.id]}",
                                "is_error": True,
                            }
                        )
                        continue

                    result = results_by_id[tool_block.id]

                    if inner_name == "search_tools" and inner_args.get("action") == "add":
                        for t_name in parse_names(inner_args.get("names")):
                            if t_name in TOOL_CATALOG:
                                tools.setdefault(t_name, get_tool_func(t_name))
                            elif t_name in MCP_RESOLVED and t_name not in resolved_mcp_names:
                                _register_mcp(t_name, MCP_RESOLVED[t_name]["routing"])

                    # Keyed on result shape, not tool name: images go in as real tool_result image blocks.
                    is_image_result = isinstance(result, dict) and result.get("type") == "image"
                    is_display_image_result = isinstance(result, dict) and result.get("display_image") is not None

                    if is_image_result:
                        yield_output, yield_image_data = "[image attached]", result["data"]
                    elif is_display_image_result:
                        yield_output, yield_image_data = str(result.get("output", ""))[:500], result["display_image"]
                    else:
                        yield_output, yield_image_data = str(result)[:500], None

                    yield {
                        "type": "tool_result",
                        "name": inner_name,
                        "output": yield_output,  # Truncated for display
                        "image_data": yield_image_data,
                        "id": tool_block.id,
                    }

                    if is_image_result:
                        tool_result_blocks.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_block.id,
                                "content": [
                                    {
                                        "type": "image",
                                        "source": {"type": "base64", "media_type": "image/jpeg", "data": result["data"]},
                                    }
                                ],
                            }
                        )
                        continue

                    # display_image is UI-only: the API gets the 'output' text, never the base64.
                    if is_display_image_result:
                        content = str(result.get("output", ""))
                    elif isinstance(result, dict):
                        content = str(result)
                    else:
                        content = result or ""

                    cap = cap_for(inner_name)
                    if len(content) > cap:
                        content = persist_and_preview(content, tool_block.id, project_dir, cap)
                        persisted_this_turn.add(tool_block.id)
                    tool_result_blocks.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_block.id,
                            "content": content,
                        }
                    )

                # Per-turn cap: persist the largest results first until under budget.
                total_chars = sum(len(b["content"]) for b in tool_result_blocks)
                if total_chars > MAX_TOOL_RESULTS_PER_MESSAGE:
                    by_id = {tb.id: tb for tb in tool_use_blocks}
                    order = sorted(
                        range(len(tool_result_blocks)),
                        key=lambda i: len(tool_result_blocks[i]["content"]),
                        reverse=True,
                    )
                    for i in order:
                        if total_chars <= MAX_TOOL_RESULTS_PER_MESSAGE:
                            break
                        block = tool_result_blocks[i]
                        tb = by_id.get(block["tool_use_id"])
                        if (tb is None or tb.id in persisted_this_turn or tb.id in unknown_ids
                                or tb.id in ask_errors_by_id or tb.id in skip_ids):
                            continue
                        old_len = len(block["content"])
                        block["content"] = persist_and_preview(
                            block["content"], tb.id, project_dir, cap_for(calls[tb.id][0])
                        )
                        persisted_this_turn.add(tb.id)
                        total_chars += len(block["content"]) - old_len

                msgs.append({"role": "user", "content": tool_result_blocks})
                pending = None

                # The one safe point to splice plain user messages between tool rounds.
                yield {"type": "turn_boundary"}

            if not tool_use_blocks and text_block:
                msgs.append({"role": "assistant", "content": text_block.text})
                yield {"type": "final_text"}
                break

            if not tool_use_blocks and not text_block:
                # 'refusal' is a normal response with zero content blocks: report it.
                if response.stop_reason == "refusal":
                    category = getattr(response.stop_details, "category", None)
                    yield {
                        "type": "error",
                        "message": "Claude declined to continue with this request"
                        + (f" (flagged: {category})." if category else "."),
                    }
                else:
                    yield {"type": "final_text"}
                break

        except asyncio.CancelledError:
            break
        except Exception as e:
            yield {
                "type": "error",
                "message": f"{type(e).__name__}: {e}",
            }
            break
        finally:
            if pending:
                msgs.append({"role": "user", "content": _unanswered_results(*pending)})
                store_msgs(project_dir, msgs)

    yield {"type": "done"}
