# Models

Application code calls `from micro_cc.models import model_call`. It should not
import a provider or decode an SDK response.

```text
claude_loop_ / summary / memory review / vision / image captions
                         │
                    model_call                 client.py
                         │                     lazy provider selection
          ┌──────────────┼─────────────────────────┐
       anthropic     foundry     litellm / openai / ollama / openrouter
          │              │                │
          └──────┬───────┘                ├── adapters/chat.py
         adapters/anthropic.py            └── adapters/responses.py
                         │
                 Response / StreamEvent        types.py
```

- **`client.py`** selects a provider using the existing endpoint precedence. An
  explicit `endpoint=` pins it for a turn. Imports stay lazy; there is no SDK
  singleton, background task, extra buffering, or second retry layer.
- **`registry.py`** is the model catalog: aliases, backend IDs, capabilities,
  context windows and UI options. It does not load providers or call APIs.
- **Provider modules** own authentication, request conversion, cache policy,
  generation defaults and request retries. Request converters remain separate
  where similar-looking protocols differ (signed thinking, explicit OpenAI
  caching, proxy metadata, Ollama options, OpenRouter's unified `reasoning`
  param).
- **`adapters/`** decode SDK output by wire protocol, not by provider. They have
  no SDK imports. Chat reasoning extraction is selected explicitly by providers.
- **`types.py`** defines the application's response and event shapes. Non-stream
  calls return `Response(content, usage)` or `None` after exhausted retries.
  Streams emit text/thinking deltas followed by a final response event. Usage
  is an `input`/`output` dictionary in both paths; input includes cached tokens.
  Anthropic's non-stream SDK content blocks are retained intact, but its outer
  Message/usage object is normalized. SDK-only response metadata is not exposed.
- **`schema.py`** converts Python functions into tool schemas and translates
  those schemas into Chat Completions/Responses tool formats. It is unrelated
  to response types.

Streams are consumed in the caller's task. Cancellation and iteration errors
propagate from adapters; a partial stream is not retried. This refactor does not
change HTTP client/stream closing policy, provider timeouts, malformed-output
policy, or compaction task ownership. Tests use SDK-shaped fixtures, not live APIs.

```python
response = await model_call(messages, model="sonnet-5")
stream = await model_call(messages, model="sonnet-5", stream=True)
```

Omit model/output limits to retain each provider's existing defaults (including
Ollama environment settings). Supplying a model passes its alias unchanged to
the provider, which resolves it through the registry. No automatic model fallback
is introduced. Provider call functions remain available, but internal consumers
use the package entry point.

Run regression tests: `env/bin/python -m unittest discover -s tests -p 'test_*.py'`.
