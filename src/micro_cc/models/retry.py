"""Request retries and first-event stream priming, shared by every api module.

A stream that connects but never sends a byte is retried like any failed request,
because prime_stream awaits its first event inside the retry loop. Once that event
is through, iteration/cancellation belong to the caller: no retry after visible output.
"""

import asyncio
import os

BASE_SLEEP = 2


def first_event_timeout(default: float) -> float:
    """The provider's first-event budget unless MICROCC_FIRST_EVENT_TIMEOUT overrides it.
    A timeout re-sends the whole prompt, so too tight a budget turns slow calls into heavy retries."""
    raw = os.getenv("MICROCC_FIRST_EVENT_TIMEOUT")
    if raw:
        try:
            return max(1.0, float(raw))
        except ValueError:
            pass
    return default


async def prime_stream(gen, timeout: float):
    """Await `gen`'s first item under `timeout`; return a generator that replays it then continues.
    Raises TimeoutError (closing `gen` first); CancelledError passes through untouched."""
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


async def retry_call(send, *, stream, decode, decode_stream, timeout, retries, label):
    """Run `send()` up to `retries` times with exponential backoff; None after the last failure."""
    for attempt in range(retries):
        try:
            raw = await send()
            if stream:
                return await prime_stream(decode_stream(raw), timeout)
            return decode(raw)
        except Exception as e:
            print(f"\n[{label}]: {e}", flush=True)
            if attempt < retries - 1:
                sleep_time = BASE_SLEEP * (2 ** attempt)
                print(f"\n[{label}]: Retrying in {sleep_time}s (attempt {attempt + 1}/{retries})...")
                await asyncio.sleep(sleep_time)
            else:
                print(f"\n[{label}]: Failed after {retries} attempts")
    return None
