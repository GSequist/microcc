"""Single entry point for conversational, background, and vision calls."""

from importlib import import_module

from micro_cc.models.types import Response, ResponseStream

# Import only the selected SDK. The registry describes models, not transports.
_PROVIDERS = {
    "Anthropic": ("anthropic", "a_model_call"),
    "Foundry": ("foundry", "f_model_call"),
    "LiteLLM": ("litellm", "l_model_call"),
    "OpenAI": ("openai", "oai_model_call"),
    "OpenRouter": ("openrouter", "or_model_call"),
    "Ollama": ("ollama", "o_model_call"),
}


async def model_call(
    input: list[dict] | str,
    model: str | None = None,
    *,
    endpoint: str | None = None,
    encoded_image: str | list[str] | None = None,
    tools: list[dict] | None = None,
    stream: bool = False,
    thinking: bool = False,
    max_tokens: int | None = None,
    client_timeout: int = 480,
    pdf: str | None = None,
    retries: int = 3,
) -> Response | ResponseStream | None:
    """Call the configured provider with the application's message format.

    Non-stream calls return Response; streams yield deltas and a final response
    event. None means the provider exhausted its request retries. Iteration and
    cancellation remain in the caller's task; this layer adds no retries/tasks.

    An explicit endpoint pins routing for a conversation turn. Otherwise it is
    resolved at call time (including background work after a login change).
    Omitted model/output limits keep provider defaults, including Ollama env
    settings. Unknown endpoint names fail instead of silently billing another
    provider. Model alias resolution remains in each provider via registry.py.
    """
    if endpoint is None:
        from micro_cc.utils.helpers import get_endpoint
        endpoint = get_endpoint()
    try:
        module_name, function_name = _PROVIDERS[endpoint]
    except KeyError:
        raise ValueError(f"Unknown model endpoint: {endpoint!r}") from None
    provider = getattr(import_module(f"micro_cc.models.{module_name}"), function_name)
    kwargs = {
        "input": input,
        "encoded_image": encoded_image,
        "tools": tools,
        "stream": stream,
        "thinking": thinking,
        "client_timeout": client_timeout,
        "pdf": pdf,
        "retries": retries,
    }
    if model is not None:
        kwargs["model"] = model
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    return await provider(**kwargs)
