# Models

Application code calls `from micro_cc.models import model_call`. It never imports
an SDK or decodes a provider response.

Three concepts, kept apart:

| term | what it is | examples |
|---|---|---|
| **provider** | who you authenticate with: env vars, base URL, auth, compat flags | `anthropic`, `claude-code`, `bedrock`, `vertex`, `foundry`, `litellm`, `openai`, `azure`, `openrouter`, `groq`, `deepseek`, `xai`, `together`, `fireworks`, `ollama` |
| **api** | the wire protocol a request is built for | `anthropic-messages`, `openai-chat`, `openai-responses` |
| **model** | an alias plus capabilities and one id per provider that serves it | `sonnet-5.5` → `claude-sonnet-5-5` on anthropic |

```text
claude_loop_ / summary / memory review / vision / image captions
                         │
                    model_call                       client.py
                         │   provider = active_provider()        providers.py
                         │   (id, api) = resolve(alias, provider) catalog.py
          ┌──────────────┼───────────────────┐
 apis/anthropic_messages  apis/openai_chat  apis/openai_responses
          │                    │                    │
 adapters/anthropic       adapters/chat       adapters/responses
                         │
                 Response / StreamEvent              types.py
```

- **`catalog.json`** is the source of truth: providers, models, login prompts. The
  website's model table reads the same file from GitHub.
- **`catalog.py`** merges the layers (packaged → `~/.micro-cc/cache/catalog.json` →
  legacy `~/.micro-cc/models.json` → `~/.micro-cc/catalog.json`) once at import, with
  no network access. It also holds the lookups (`resolve`, `models_for`, effort,
  budgets). A user can add an OpenAI-compatible provider purely in their own catalog.json.
- **`catalog_sync.py`** refreshes the cache from GitHub in a daemon thread started by
  the supervisor only, with an atomic write.
- **`providers.py`** picks the active provider. `MICROCC_PROVIDER` (written by `/login`)
  wins when that provider's `requires` env vars are set. Otherwise the first configured
  provider in `routing` order wins.
- **`apis/`** build requests per wire protocol. Provider differences are data in
  `compat` (betas, reasoning dialect, cache flags) or `client` (the SDK's Bedrock/Vertex
  clients), not separate modules. A provider's optional SDK dependency (`package`, e.g.
  google-auth for Vertex) is installed on demand at `/login` and again at call time if missing.
- **Credentials** live in `~/.micro-cc/.env` (0600, rewritten atomically by `/login`). Keys a
  child can reload from that file are hidden from the model's `bash_` shell.
- **`adapters/`** decode SDK output per protocol. They have no SDK imports.
- **`retry.py`** holds the shared retry loop and first-event stream priming. There is no
  retry after visible output.
- **`schema.py`** turns Python functions into tool schemas.

`models.json` (schema 1) is frozen: installs older than catalog.json still fetch it
from GitHub main, so it stays, but it no longer changes. New models reach updated
installs only.

```python
response = await model_call(messages, model="sonnet-5.5")
stream = await model_call(messages, model="gpt-6-sol", provider="litellm", stream=True)
```

Run regression tests: `env/bin/python tests/test_model_catalog.py`.
