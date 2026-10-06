"""Login and /keys input handling for MicroTui; module functions taking the app."""

import asyncio
import os
import threading

from micro_cc.models import registry as model_registry
from micro_cc.tui_native.list_picker_ import PickerItem
from micro_cc.utils import settings_store_
from micro_cc.utils.claude_subscription import discover_claude_code_token, oauth_login_flow
from micro_cc.utils.helpers import (
    apply_login,
    apply_project_keys,
    check_ollama,
    check_openrouter,
    compute_ollama_trim_budget,
)


def ollama_trim_budget(num_ctx: int = None, max_output: int = None) -> int:
    """Get trim budget from MICRO_CC_TRIM_BUDGET env var or derive from context."""
    override = os.getenv("MICRO_CC_TRIM_BUDGET")
    if override:
        return int(override)
    if num_ctx is None:
        num_ctx = int(os.getenv("OLLAMA_NUM_CTX", "32768"))
    if max_output is None:
        max_output = int(os.getenv("OLLAMA_MAX_OUTPUT", "4096"))
    return compute_ollama_trim_budget(num_ctx, max_output)



def on_login_picked(app, item: PickerItem) -> None:
    provider = item.value
    app._close_picker("_login_picker_overlay")
    if provider == "Bad Bunny":
        # Runs as its own task, not via _login_stage machinery.
        app.set_focus(app.prompt)
        app._safe_task(app._claude_code_login(), "OAuth login")
        return
    app._login_provider = provider
    app._login_stage = app.LOGIN_SEQUENCE[provider][0]
    app._input_mode = "login"
    app.prompt.placeholder = app.LOGIN_PROMPTS[app._login_stage]
    app._refresh_status()
    app.set_focus(app.prompt)

async def claude_code_login(app) -> None:
    """Handle OAuth authentication via Bad Bunny (Claude Code)."""
    app._input_mode = "query_active"
    app._oauth_cancel_event = threading.Event()
    try:
        token = discover_claude_code_token()
        if token:
            app._flash_status("found existing Bad Bunny login")
        else:
            app._flash_status("opening browser for Bad Bunny login...")
            token = await oauth_login_flow(cancel_event=app._oauth_cancel_event)
        apply_login({"ANTHROPIC_OAUTH_TOKEN": token})

        # OAuth reaches Anthropic models same as pasted API key; refresh picker options.
        options = model_registry.options_for_backend("anthropic")
        app.model_picker.set_items([PickerItem(o, o) for o in options])
        if app._current_model not in options:
            app._current_model = model_registry.DEFAULT_MODEL
            settings_store_.edit_setting("model", app._current_model)
        app._trim_budget = model_registry.trim_budget_for(app._current_model)

        app._flash_status("Bad Bunny configured")
    except asyncio.CancelledError:
        app._oauth_cancel_event.set()
        app._flash_status("Bad Bunny login cancelled")
        raise
    except Exception as e:
        app._flash_status(
            f"Bad Bunny login failed: {e}"
        )
    finally:
        app._input_mode = "idle"
        app._refresh_status()

def handle_login_input(app, query: str) -> None:
    key_name = app._login_stage

    # OLLAMA_BASE_URL must land in .env even if blank (apply_login filters blanks).
    if app._login_provider == "Ollama" and key_name == "OLLAMA_BASE_URL":
        query = query.strip() or "http://localhost:11434/v1"

    app._login_values[key_name] = query

    if app._login_provider == "Ollama" and key_name == "OLLAMA_MODEL":
        model = query.strip()
        error = check_ollama(model, app._login_values.get("OLLAMA_BASE_URL"))
        if error:
            # Bounce back to same field to retype, not restart /login.
            app._flash_status(error)
            app._login_values.pop(key_name, None)
            app.prompt.placeholder = app.LOGIN_PROMPTS[
                "OLLAMA_MODEL"
            ]
            return

    if app._login_provider == "OpenRouter" and key_name == "OPENROUTER_MODEL":
        model = query.strip()
        error = check_openrouter(model)
        if error:
            app._flash_status(error)
            app._login_values.pop(key_name, None)
            app.prompt.placeholder = app.LOGIN_PROMPTS[
                "OPENROUTER_MODEL"
            ]
            return

    sequence = app.LOGIN_SEQUENCE[app._login_provider]
    next_index = sequence.index(key_name) + 1

    if next_index < len(sequence):
        app._login_stage = sequence[next_index]
        app.prompt.placeholder = app.LOGIN_PROMPTS[
            app._login_stage
        ]
        # Confirm field registered before box goes blank for next stage.
        app._flash_status(f"✓ {key_name} saved")
        return

    if app._login_provider == "Ollama":
        model = app._login_values.get("OLLAMA_MODEL", "").strip()
        # Set instance attr (not class attr); app already exists.
        app._current_model = model
        # Update settings.json so statusline picks up model change.
        settings_store_.edit_setting("model", model)
        # Derive budget from entered NUM_CTX/MAX_OUTPUT; fixed budget would overflow small context.
        num_ctx_input = app._login_values.get("OLLAMA_NUM_CTX", "").strip()
        max_output_input = app._login_values.get("OLLAMA_MAX_OUTPUT", "").strip()
        app._trim_budget = ollama_trim_budget(
            num_ctx=int(num_ctx_input) if num_ctx_input else None,
            max_output=int(max_output_input) if max_output_input else None,
        )
        app.model_picker.set_items([PickerItem(model, model)])
    elif app._login_provider == "OpenRouter":
        # No fixed alias table — free-text model, same as Ollama above.
        model = app._login_values.get("OPENROUTER_MODEL", "").strip()
        app._current_model = model
        settings_store_.edit_setting("model", model)
        app._trim_budget = model_registry.trim_budget_for(model, backend="openrouter")
        app.model_picker.set_items([PickerItem(model, model)])
    elif app._login_provider in ("Anthropic", "Foundry", "LiteLLM", "OpenAI"):
        # Filter picker to backend-specific models (some backends can't reach all aliases).
        backend_key = {
            "Anthropic": "anthropic",
            "Foundry": "foundry",
            "LiteLLM": "litellm",
            "OpenAI": "openai",
        }[app._login_provider]
        options = model_registry.options_for_backend(backend_key)
        app.model_picker.set_items([PickerItem(o, o) for o in options])
        if app._current_model not in options:
            app._current_model = model_registry.DEFAULT_MODEL
            # Update settings.json to avoid reloading stale pre-switch model on next launch.
            settings_store_.edit_setting("model", app._current_model)
        app._trim_budget = model_registry.trim_budget_for(app._current_model)

    apply_login(
        {k: v for k, v in app._login_values.items() if v}
    )  # drop blanks (skipped SERPAPI)
    app._flash_status(f"{app._login_provider} configured")
    app._login_stage = None
    app._login_provider = None
    app._login_values = {}
    app._input_mode = "idle"
    app.prompt.placeholder = ""
    app._refresh_status()

def handle_keys_input(app, query: str) -> None:

    if app._keys_stage == "name":
        name = query.strip()
        if not name:  # blank name ends the loop
            if app._keys_values:
                apply_project_keys(app._project_dir, app._keys_values)
                app._flash_status(
                    f"Stored {', '.join(app._keys_values)} in "
                    f"{app._project_dir}/.env",
                )
            else:
                app._flash_status("No keys stored")
            app._keys_stage = None
            app._keys_pending_name = None
            app._keys_values = {}
            app._input_mode = "idle"
            app.prompt.placeholder = ""
            return
        app._keys_pending_name = name
        app._keys_stage = "value"
        app.prompt.placeholder = f"Value for {name}"
        return

    # stage == "value"
    app._keys_values[app._keys_pending_name] = query
    app._keys_pending_name = None
    app._keys_stage = "name"
    app.prompt.placeholder = "Another env var name — blank to finish"
