import asyncio
import contextvars
import inspect
import threading
from typing import Any, Dict

# Backstop for a stuck sync tool (FIFO, hung mount); tools with their own deadlines return earlier.
SYNC_TOOL_TIMEOUT = 120


def _run_in_daemon_thread(fn, kwargs: dict) -> asyncio.Future:
    """Run a sync tool on a daemon thread (not to_thread: executor threads block process exit)."""
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    ctx = contextvars.copy_context()

    def _settle(result, exc):
        if fut.done():
            return
        if exc is not None:
            fut.set_exception(exc)
        else:
            fut.set_result(result)

    def _worker():
        try:
            result, exc = ctx.run(fn, **kwargs), None
        except BaseException as e:
            result, exc = None, e
        try:
            loop.call_soon_threadsafe(_settle, result, exc)
        except RuntimeError:
            pass  # loop already closed — process is exiting

    threading.Thread(target=_worker, daemon=True, name=f"tool:{getattr(fn, '__name__', 'fn')}").start()
    return fut


async def execute_tool_call(
    tool_call,
    tools: Dict[str, callable],
    project_dir: str,
    model: str,
):
    """Execute a tool call; sync tools run on a daemon thread with timeout."""
    name = tool_call.name
    args = tool_call.input

    if name not in tools:
        return (
            f"Tool '{name}' is not available. Call search_tools(action=\"discover\") "
            f"to see what's available, then search_tools(action=\"add\", "
            f"names=\"{name}\") to load it, then call it via use_tool_."
        )

    tool = tools[name]

    try:
        sig = inspect.signature(tool)
        if "project_dir" in sig.parameters:
            args = {**args, "project_dir": project_dir}
        if "model" in sig.parameters:
            args = {**args, "model": model}

        if inspect.iscoroutinefunction(tool):
            return await tool(**args)

        try:
            result = await asyncio.wait_for(_run_in_daemon_thread(tool, args), SYNC_TOOL_TIMEOUT)
        except asyncio.TimeoutError:
            return (
                f"[{name} did not return within {SYNC_TOOL_TIMEOUT}s and was abandoned — it may "
                f"still be running in the background. The path/target may be blocking "
                f"(FIFO, device, network mount) or the scope too large. Inspect it with "
                f"bash_ (e.g. `ls -la`, `file`, `timeout 30 ...`) before retrying; if this "
                f"was a write_/edit_, verify the file's state before writing again.]"
            )
        if inspect.isawaitable(result):
            result = await result
        return result

    except Exception as e:
        return f"Tool error ({name}): {type(e).__name__}: {e}"
