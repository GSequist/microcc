"""Login and /keys input handling for MicroTui; module functions taking the app."""

import asyncio
import threading

from micro_cc.models import catalog as model_catalog
from micro_cc.models.providers import ensure_package
from micro_cc.tui_native.option_panel_ import PanelItem
from micro_cc.utils import settings_store_
from micro_cc.utils.helpers import (
    apply_login,
    apply_project_keys,
    check_ollama,
    check_openrouter,
)


def on_login_picked(app, item: PanelItem) -> None:
    provider = item.value
    if model_catalog.PROVIDERS[provider].get("auth") == "oauth":
        # Runs as its own task, not via _login_stage machinery.
        app._safe_task(claude_code_login(app, provider), "OAuth login")
        return
    if model_catalog.PROVIDERS[provider].get("package"):
        app._safe_task(_install_then_login(app, provider), "provider dependency install")
        return
    _start_login(app, provider)


async def _install_then_login(app, provider: str) -> None:
    """Install the provider's optional dependency (e.g. google-auth for Vertex), then ask for its keys."""
    pip = model_catalog.PROVIDERS[provider]["package"]["pip"]
    app._flash_status(f"installing {pip} for {model_catalog.provider_name(provider)}…")
    error = await asyncio.to_thread(ensure_package, provider)
    if error:
        app._flash_status(f"install failed: {error}")
        app._input_mode = "idle"
        app._refresh_status()
        return
    _start_login(app, provider)


def _start_login(app, provider: str) -> None:
    app._login_provider = provider
    app._login_stage = model_catalog.login_sequence(provider)[0]
    app._input_mode = "login"
    app.prompt.placeholder = model_catalog.login_prompt(app._login_stage)
    app._refresh_status()


def _keep_or_reset_model(app, provider: str) -> None:
    """Fall back to the default model when the current one isn't served by `provider`."""
    if app._current_model not in model_catalog.models_for(provider, app._current_model):
        app._current_model = model_catalog.DEFAULT_MODEL
        # Persist so the next launch doesn't reload a model this provider can't serve.
        settings_store_.edit_setting("model", app._current_model)

async def claude_code_login(app, provider: str = "claude-code") -> None:
    """Handle OAuth authentication via Claude Code."""
    app._input_mode = "query_active"
    from micro_cc.utils.claude_subscription import discover_claude_code_token, oauth_login_flow  # lazy: pulls httpx

    app._oauth_cancel_event = threading.Event()
    try:
        token = discover_claude_code_token()
        if token:
            app._flash_status("found existing Claude Code login")
        else:
            app._flash_status("opening browser for Claude Code login...")
            token = await oauth_login_flow(cancel_event=app._oauth_cancel_event)
        apply_login({"ANTHROPIC_OAUTH_TOKEN": token}, provider)
        _keep_or_reset_model(app, provider)
        app._trim_budget = model_catalog.effective_trim_budget(app._current_model, provider)

        app._flash_status("Claude Code configured")
    except asyncio.CancelledError:
        app._oauth_cancel_event.set()
        app._flash_status("Claude Code login cancelled")
        raise
    except Exception as e:
        app._flash_status(
            f"Claude Code login failed: {e}"
        )
    finally:
        app._input_mode = "idle"
        app._refresh_status()

def handle_login_input(app, query: str) -> None:
    key_name = app._login_stage

    # OLLAMA_BASE_URL must land in .env even if blank (apply_login filters blanks).
    if app._login_provider == "ollama" and key_name == "OLLAMA_BASE_URL":
        query = query.strip() or "http://localhost:11434/v1"

    app._login_values[key_name] = query

    if app._login_provider == "ollama" and key_name == "OLLAMA_MODEL":
        model = query.strip()
        error = check_ollama(model, app._login_values.get("OLLAMA_BASE_URL"))
        if error:
            # Bounce back to same field to retype, not restart /login.
            app._flash_status(error)
            app._login_values.pop(key_name, None)
            app.prompt.placeholder = model_catalog.login_prompt("OLLAMA_MODEL")
            return

    if app._login_provider == "openrouter" and key_name == "OPENROUTER_MODEL":
        model = query.strip()
        error = check_openrouter(model)
        if error:
            app._flash_status(error)
            app._login_values.pop(key_name, None)
            app.prompt.placeholder = model_catalog.login_prompt("OPENROUTER_MODEL")
            return

    sequence = model_catalog.login_sequence(app._login_provider)
    next_index = sequence.index(key_name) + 1

    if next_index < len(sequence):
        app._login_stage = sequence[next_index]
        app.prompt.placeholder = model_catalog.login_prompt(app._login_stage)
        # Confirm field registered before box goes blank for next stage.
        app._flash_status(f"✓ {key_name} saved")
        return

    provider = app._login_provider
    apply_login({k: v for k, v in app._login_values.items() if v}, provider)  # drop blanks (skipped SERPAPI)
    model_env = model_catalog.PROVIDERS[provider].get("model_env")
    if model_env:
        # Free-text provider (Ollama, OpenRouter): the model is whatever was typed at login.
        app._current_model = app._login_values.get(model_env, "").strip()
        settings_store_.edit_setting("model", app._current_model)
    else:
        _keep_or_reset_model(app, provider)
    # Ollama's budget comes from the NUM_CTX/MAX_OUTPUT just written to the env.
    app._trim_budget = model_catalog.effective_trim_budget(app._current_model, provider)
    app._flash_status(f"{model_catalog.provider_name(provider)} configured")
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
