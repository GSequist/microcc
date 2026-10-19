"""Single entry point for conversational, background, and vision calls."""

import asyncio
import weakref
from importlib import import_module

from micro_cc.models.types import Response, ResponseStream

# Wire protocol -> module under models/apis. Only the selected SDK gets imported.
_APIS = {
    "anthropic-messages": "anthropic_messages",
    "openai-chat": "openai_chat",
    "openai-responses": "openai_responses",
}


# loop -> {key: SDK client}; an httpx pool is bound to the loop that opened it.
_CLIENTS: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def cached_client(key: tuple, build):
    """Reuse one SDK client (and its warm connections) per key on the running loop."""
    per_loop = _CLIENTS.setdefault(asyncio.get_running_loop(), {})
    if key not in per_loop:
        per_loop[key] = build()
    return per_loop[key]


async def prewarm(model: str | None = None) -> None:
    """Import the active provider's SDK and open its connection so the first message skips both."""
    try:
        from micro_cc.models import catalog, providers

        provider = providers.active_provider()
        alias = model or providers.default_model(provider)
        _model_id, api = catalog.resolve(alias, provider)
        module = await asyncio.to_thread(import_module, f"micro_cc.models.apis.{_APIS[api]}")
        client = module.client_for(provider, alias)
        # Unauthenticated HEAD: opens DNS/TCP/TLS into the pool, bills nothing.
        await asyncio.wait_for(client._client.head(str(client.base_url)), timeout=10)
    except Exception:
        pass


async def model_call(
    input: list[dict] | str,
    model: str | None = None,
    *,
    provider: str | None = None,
    encoded_image: str | list[str] | None = None,
    tools: list[dict] | None = None,
    stream: bool = False,
    thinking: bool = False,
    reasoning_effort: str | None = None,
    max_tokens: int | None = None,
    client_timeout: int = 480,
    pdf: str | None = None,
    retries: int = 3,
) -> Response | ResponseStream | None:
    """Call `model` on `provider` (default: the active one) with the application's message format.

    Non-stream calls return Response; streams yield deltas then a final response event.
    None means the request retries were exhausted. An explicit provider pins routing for a
    turn; unknown providers fail instead of silently billing another one.
    """
    from micro_cc.models import catalog, providers

    provider = provider or providers.active_provider()
    if provider not in catalog.PROVIDERS:
        raise ValueError(f"Unknown model provider: {provider!r}")
    alias = model or providers.default_model(provider)
    model_id, api = catalog.resolve(alias, provider)
    module = import_module(f"micro_cc.models.apis.{_APIS[api]}")
    return await module.call(
        provider, alias, model_id,
        input=input,
        encoded_image=encoded_image,
        tools=tools,
        stream=stream,
        thinking=thinking,
        reasoning_effort=reasoning_effort,
        max_tokens=max_tokens,
        client_timeout=client_timeout,
        pdf=pdf,
        retries=retries,
    )
