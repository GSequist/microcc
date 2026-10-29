"""Request retries and first-event stream priming, shared by every api module.

A stream that connects but never sends a byte is retried like any failed request,
because prime_stream awaits its first event inside the retry loop. A transient failure
after that (dropped connection, 5xx, overloaded) re-sends the whole request and yields a
`stream_reset` event first, so the caller discards the partial turn before the replay.
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


_TRANSIENT_NAMES = {"APIConnectionError", "APITimeoutError", "InternalServerError", "OverloadedError",
                    "RateLimitError", "ServiceUnavailableError", "APIError"}  # bare APIError = in-stream error event
_TRANSIENT_TYPES = {"overloaded_error", "api_error", "server_error", "rate_limit_error", "timeout"}


def is_transient(e: BaseException) -> bool:
    """Connection/timeout/5xx/429/overloaded; auth and other 4xx are not worth a re-send."""
    import httpx
    if isinstance(e, (asyncio.TimeoutError, ConnectionError, httpx.TransportError)):
        return True
    status = getattr(e, "status_code", None)
    if isinstance(status, int) and status != 200:  # in-stream errors arrive on a 200 response
        return status in (408, 429) or status >= 500
    body = getattr(e, "body", None)
    err = body.get("error", body) if isinstance(body, dict) else None
    if isinstance(err, dict):
        code = err.get("code")
        if isinstance(code, int):
            return code in (408, 429) or code >= 500
        if err.get("type") in _TRANSIENT_TYPES or code in _TRANSIENT_TYPES:
            return True
    return type(e).__name__ in _TRANSIENT_NAMES


async def _open(send, *, stream, decode, decode_stream, timeout, retries, label):
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


async def _resilient(gen, reopen, retries, label):
    """Iterate `gen`; on a transient mid-stream failure yield stream_reset and continue on a fresh stream."""
    resets = 0
    while True:
        try:
            async for event in gen:
                yield event
            return
        except Exception as e:
            if resets >= retries or not is_transient(e):
                raise
            resets += 1
            print(f"\n[{label}]: stream dropped mid-turn: {e}; re-sending ({resets}/{retries})", flush=True)
            yield {"type": "stream_reset", "error": f"{type(e).__name__}: {e}", "attempt": resets}
            await asyncio.sleep(BASE_SLEEP * (2 ** (resets - 1)))
            gen = await reopen()
            if gen is None:
                raise e


async def retry_call(send, *, stream, decode, decode_stream, timeout, retries, label):
    """Open the call with retries; a stream is also re-sent (bounded) if it dies mid-turn. None if exhausted."""
    reopen = lambda: _open(send, stream=stream, decode=decode, decode_stream=decode_stream,
                           timeout=timeout, retries=retries, label=label)
    first = await reopen()
    if not stream or first is None:
        return first
    return _resilient(first, reopen, retries, label)
