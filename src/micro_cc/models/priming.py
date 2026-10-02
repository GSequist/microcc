"""First-event stream priming.

Used only inside each provider file's own retry loop (anthropic.py,
foundry.py, litellm.py, ollama.py, openai.py) — not a new retry layer.
Streaming SDK calls return as soon as the request is accepted; if the
connection then just sits there and never sends a single byte, that hang
previously surfaced only once the caller (claude_loop_'s `async for event in
stream`) started consuming — outside any provider retry loop, so it was
never retried, just a single dead air wait until client_timeout.

prime_stream fetches the stream's first event while still inside the
provider's own `for attempt in range(retries)` loop, so a stream that never
produces anything gets treated like any other failed attempt and retried.
Once the first event is through, iteration/cancellation are the caller's
task again, unchanged — see design/architecture.toml's
model-response-boundary contract: no retry after visible output.
"""

import asyncio
import os


def first_event_timeout(default: float) -> float:
    """The provider's first-event budget, unless MICROCC_FIRST_EVENT_TIMEOUT
    (seconds) overrides it for every provider. A timeout re-sends the WHOLE
    prompt, so too tight a budget on a slow proxy (litellm cold start, a long
    subagent system prompt + task, high reasoning effort before the first
    summary delta) turns a slow-but-fine call into repeated heavy retries."""
    raw = os.getenv("MICROCC_FIRST_EVENT_TIMEOUT")
    if raw:
        try:
            return max(1.0, float(raw))
        except ValueError:
            pass
    return default


async def prime_stream(gen, timeout: float):
    """Await `gen`'s first item under `timeout`; return a generator that
    replays it then continues. Raises TimeoutError (closing `gen` first) if
    nothing arrives in time. Real cancellation (asyncio.CancelledError) is
    not a TimeoutError and passes through untouched, same as before."""
    try:
        first = await asyncio.wait_for(anext(gen), timeout=timeout)
    except asyncio.TimeoutError:
        await gen.aclose()
        raise

    async def _replay():
        yield first
        async for event in gen:
            yield event

    return _replay()
