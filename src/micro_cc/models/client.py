"""Single entry point for conversational, background, and vision calls."""

from importlib import import_module

from micro_cc.models.types import Response, ResponseStream

# Wire protocol -> module under models/apis. Only the selected SDK gets imported.
_APIS = {
    "anthropic-messages": "anthropic_messages",
    "openai-chat": "openai_chat",
    "openai-responses": "openai_responses",
}


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
