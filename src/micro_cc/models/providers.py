"""Which provider is active, and its credentials, from env vars described in catalog.json."""

import importlib
import os
import subprocess

from dotenv import dotenv_values, load_dotenv

from micro_cc.models.catalog import LOGIN_OPTIONAL, PROVIDERS, ROUTING

ENV_PATH = os.path.expanduser("~/.micro-cc/.env")


def _owner_only(path: str) -> None:
    """Tighten a group/world-readable key file (written by older versions) to 0600."""
    try:
        if os.stat(path).st_mode & 0o077:
            os.chmod(path, 0o600)
    except OSError:
        pass


_owner_only(ENV_PATH)
load_dotenv(ENV_PATH)
load_dotenv()

# Written by /login next to the keys, so a stray shell key can't reroute an explicit choice.
PROVIDER_ENV = "MICROCC_PROVIDER"
DEFAULT_PROVIDER = "anthropic"


def is_configured(provider: str) -> bool:
    required = PROVIDERS.get(provider, {}).get("requires")
    return bool(required) and all(os.getenv(k) for k in required)


def active_provider() -> str:
    """MICROCC_PROVIDER if its keys are present, else the first configured provider in routing order."""
    chosen = os.getenv(PROVIDER_ENV)
    if chosen in PROVIDERS and is_configured(chosen):
        return chosen
    return next((p for p in ROUTING if is_configured(p)), DEFAULT_PROVIDER)


def any_configured() -> bool:
    return any(is_configured(p) for p in PROVIDERS)


def credentials(provider: str) -> tuple:
    """(base_url or None, api_key) for `provider` from its env vars and defaults."""
    p = PROVIDERS.get(provider, {})
    base_url = os.getenv(p.get("base_url_env", "")) or p.get("base_url") or None
    api_key = os.getenv(p.get("api_key_env", "")) or p.get("api_key", "")
    return base_url, api_key


def default_model(provider: str):
    """Free-text providers name their model in an env var (OLLAMA_MODEL); others use the catalog default."""
    from micro_cc.models.catalog import DEFAULT_MODEL
    env = PROVIDERS.get(provider, {}).get("model_env")
    return (os.getenv(env) if env else None) or DEFAULT_MODEL


def region(provider: str):
    p = PROVIDERS.get(provider, {})
    return os.getenv(p.get("region_env", "")) or p.get("region")


def ensure_package(provider: str) -> str | None:
    """Install the provider's optional SDK dependency (e.g. google-auth for Vertex) if missing.
    Returns an error tail, or None when it is importable. Blocking: run it off the event loop."""
    pkg = PROVIDERS.get(provider, {}).get("package")
    if not pkg:
        return None
    try:
        importlib.import_module(pkg["import"])
        return None
    except ImportError:
        pass
    from micro_cc.self_heal_ import install_cmd
    proc = subprocess.run(install_cmd(pkg["pip"], force=False), capture_output=True, text=True)
    importlib.invalidate_caches()
    if proc.returncode != 0:
        return "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-5:]) or f"pip install {pkg['pip']} failed"
    return None


def secret_env_keys() -> set:
    """Provider credential env vars to hide from the model's shell: those a child microcc can
    reload unchanged from ~/.micro-cc/.env. Keys that exist only in the launching environment
    (headless deployments) stay, so subagents started through bash_ keep working."""
    try:
        saved = dotenv_values(ENV_PATH)
    except OSError:
        return set()
    keys = {p["api_key_env"] for p in PROVIDERS.values() if "api_key_env" in p}
    return {k for k in keys if saved.get(k) and saved.get(k) == os.getenv(k)}


def routing_env_keys() -> list:
    """Env vars /login clears before writing a new provider: what routes or authenticates one.
    Settings like AWS_REGION or OLLAMA_MODEL stay, since the shell may rely on them."""
    keys = {PROVIDER_ENV}
    for p in PROVIDERS.values():
        keys.update(p.get("requires", []))
        keys.update(p[k] for k in ("base_url_env", "api_key_env") if k in p)
    return sorted(keys - set(LOGIN_OPTIONAL))
