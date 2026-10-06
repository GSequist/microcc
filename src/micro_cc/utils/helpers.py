from dotenv import load_dotenv
from PIL import Image
import base64
import hashlib
import json
import os
import io
import sys
import warnings


load_dotenv(os.path.expanduser("~/.micro-cc/.env"))
load_dotenv()

def get_endpoint() -> str:
    """Infer endpoint from env vars (proxies before bare keys)."""
    if os.getenv("OLLAMA_BASE_URL"):
        return "Ollama"
    if os.getenv("ANTHROPIC_OAUTH_TOKEN") or os.getenv("ANTHROPIC_API_KEY"):
        return "Anthropic"
    if os.getenv("FOUNDRY_BASE_URL") and os.getenv("FOUNDRY_API_KEY"):
        return "Foundry"
    if os.getenv("LITELLM_BASE_URL") and os.getenv("LITELLM_API_KEY"):
        return "LiteLLM"
    if os.getenv("OPENROUTER_API_KEY"):
        return "OpenRouter"
    if os.getenv("OPENAI_API_KEY"):
        return "OpenAI"
    return "Anthropic"


def has_configured_endpoint() -> bool:
    return bool(
        os.getenv("ANTHROPIC_OAUTH_TOKEN")
        or os.getenv("ANTHROPIC_API_KEY")
        or (os.getenv("FOUNDRY_BASE_URL") and os.getenv("FOUNDRY_API_KEY"))
        or (os.getenv("LITELLM_BASE_URL") and os.getenv("LITELLM_API_KEY"))
        or os.getenv("OLLAMA_BASE_URL")
        or os.getenv("OPENROUTER_API_KEY")
        or os.getenv("OPENAI_API_KEY")
    )


############################################################################################################

_ROUTING_KEYS = ["OLLAMA_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_OAUTH_TOKEN", "FOUNDRY_BASE_URL", "FOUNDRY_API_KEY", "LITELLM_BASE_URL", "LITELLM_API_KEY", "OPENROUTER_API_KEY", "OPENROUTER_BASE_URL", "OPENAI_BASE_URL", "OPENAI_API_KEY"]
# OPENAI_API_KEY is an ordinary routing var now (tool discovery is embedding-free), cleared on /login.


def apply_login(values: dict):
    """Recreate ~/.micro-cc/.env with new provider keys."""
    for key in _ROUTING_KEYS:
        os.environ.pop(key, None)  # clear this session's env, not just the file
    os.environ.update(values)

    env_path = os.path.expanduser("~/.micro-cc/.env")
    with open(env_path, "w") as f:
        for k, v in values.items():
            f.write(f"{k}={v}\n")


def apply_project_keys(project_dir: str, values: dict):
    """Append secrets to project .env (sourced fresh per bash call)."""
    values = {k: v for k, v in values.items() if v}
    if not values:
        return

    env_path = os.path.join(project_dir, ".env")
    with open(env_path, "a") as f:
        for k, v in values.items():
            f.write(f"{k}={v}\n")

    gitignore_path = os.path.join(project_dir, ".gitignore")
    existing_lines = []
    if os.path.isfile(gitignore_path):
        existing_lines = open(gitignore_path).read().splitlines()
    if ".env" not in existing_lines:
        with open(gitignore_path, "a") as f:
            f.write(".env\n")


############################################################################################################


def project_hash(project_dir: str) -> str:
    """Stable hash for project path (doesn't leak path in folder names)."""
    normalized = os.path.abspath(os.path.expanduser(project_dir))
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]

############################################################################################################
##dirs

WORK_FOLDER = os.path.join(os.getcwd(), "workspace/")

############################################################################################################


_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")


def _extract_one_leading_image_path(text: str):
    """Extract quoted/escaped leading image path from text."""
    stripped = text.strip()
    if not stripped:
        return text, None

    if stripped[0] in "'\"":
        quote = stripped[0]
        end = stripped.find(quote, 1)
        if end == -1:
            return text, None
        candidate, remainder = stripped[1:end], stripped[end + 1:].strip()
    else:
        i, n, raw = 0, len(stripped), []  # Parse backslash-escaped spaces only
        while i < n:
            if stripped[i] == "\\" and i + 1 < n and stripped[i + 1] == " ":
                raw.append(" ")
                i += 2
            elif stripped[i] in (" ", "\t"):
                break
            else:
                raw.append(stripped[i])
                i += 1
        candidate, remainder = "".join(raw), stripped[i:].strip()

    candidate = os.path.expanduser(candidate)
    if candidate.lower().endswith(_IMAGE_EXTENSIONS) and os.path.isfile(candidate):
        return remainder, candidate
    return text, None


def extract_image_paths(text: str):
    """Extract leading image paths; return (remaining_text, paths)."""
    paths = []
    remaining = text
    while True:
        candidate_remaining, path = _extract_one_leading_image_path(remaining)
        if path is None:
            break
        paths.append(path)
        remaining = candidate_remaining
    return (remaining if paths else text), paths


_HISTORY_IMAGE_MAX_DIMENSION = 2000   # reasonable default for max image dimension in history
_HISTORY_IMAGE_MAX_BASE64_BYTES = int(4.5 * 1024 * 1024)   # headroom under Anthropic's 5MB inline-image limit


def resize_image_for_history_(base64_data: str) -> str:
    """Resize image to fit pixel/byte limits for history."""
    raw_bytes = base64.b64decode(base64_data)
    with Image.open(io.BytesIO(raw_bytes)) as img:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            img = img.convert("RGB")
        if max(img.size) > _HISTORY_IMAGE_MAX_DIMENSION:
            scale = _HISTORY_IMAGE_MAX_DIMENSION / max(img.size)
            img = img.resize(
                (round(img.width * scale), round(img.height * scale)), Image.LANCZOS
            )
        for quality in (80, 60, 40):
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=quality)
            encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
            if len(encoded) <= _HISTORY_IMAGE_MAX_BASE64_BYTES:
                return encoded
        img = img.resize((img.width // 2, img.height // 2), Image.LANCZOS)  # Over budget: shrink and accept
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=40)
        return base64.b64encode(buffer.getvalue()).decode("ascii")


def sanitize_and_encode_image_(img_data):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            if isinstance(img_data, str) and os.path.exists(img_data):
                with Image.open(img_data) as img:
                    img = img.convert("RGB")
                    buffer = io.BytesIO()
                    img.save(buffer, format="JPEG")
                    return base64.b64encode(buffer.getvalue()).decode("utf-8")
            else:
                with Image.open(io.BytesIO(img_data)) as img:
                    img = img.convert("RGB")
                    buffer = io.BytesIO()
                    img.save(buffer, format="JPEG")
                    return base64.b64encode(buffer.getvalue()).decode("utf-8")
    except Exception:  # Silent drop: can run in alt screen
        return None

#################################


def ollama_daemon_up(base_url: str = None) -> bool:
    """Check if Ollama daemon is responding."""
    import urllib.request

    base_url = base_url or os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    tags_url = base_url.rstrip("/").removesuffix("/v1") + "/api/tags"
    try:
        urllib.request.urlopen(tags_url, timeout=2)
        return True
    except Exception:
        return False


def start_ollama_daemon(timeout: float = 10.0) -> bool:
    """Start Ollama via brew and wait until ready."""
    import subprocess
    import time

    try:
        subprocess.run(
            ["brew", "services", "start", "ollama"],
            capture_output=True, timeout=15,
        )
    except Exception:
        return False

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ollama_daemon_up():
            return True
        time.sleep(0.5)
    return False


def check_ollama(model: str, base_url: str = None) -> str:
    """Validate Ollama daemon and model (empty string = ok)."""
    import urllib.request
    import json as _json

    if not model:
        return "Enter a model tag, e.g. qwen3:14b"

    base_url = base_url or os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    tags_url = base_url.rstrip("/").removesuffix("/v1") + "/api/tags"

    try:
        with urllib.request.urlopen(tags_url, timeout=2) as resp:
            data = _json.loads(resp.read())
    except Exception:
        return (
            f"Ollama daemon not reachable at {base_url}. "
            f"Run: brew install ollama && brew services start ollama, then: ollama pull {model}"
        )

    tags = [m.get("name", "") for m in data.get("models", [])]
    if model not in tags:
        pulled = ", ".join(tags) if tags else "none"
        return f"Model '{model}' not pulled — run: ollama pull {model}  (pulled: {pulled})"

    return ""


def check_openrouter(model: str, base_url: str = None) -> str:
    """Validate model on OpenRouter (empty string = ok)."""
    import urllib.request
    import json as _json

    if not model:
        return "Enter a model slug, author/name — e.g. qwen/qwen3-235b-a22b-thinking-2507"

    base_url = (base_url or os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")).rstrip("/")

    try:
        with urllib.request.urlopen(f"{base_url}/models", timeout=5) as resp:
            data = _json.loads(resp.read())
    except Exception:
        return f"Couldn't reach OpenRouter at {base_url} to verify the model — check your connection and retry"

    ids = {m.get("id", "") for m in data.get("data", [])}
    if model not in ids:
        return f"Model '{model}' not found on OpenRouter — browse exact slugs at openrouter.ai/models"

    return ""


OLLAMA_PROMPT_RESERVE = 6000  # History only; system, tools, context uncounted
OLLAMA_MIN_TRIM_BUDGET = 2000


def compute_ollama_trim_budget(num_ctx: int, max_output: int) -> int:
    """Compute history budget from context window with safety floors."""
    return max(OLLAMA_MIN_TRIM_BUDGET, num_ctx - max_output - OLLAMA_PROMPT_RESERVE)


def effective_trim_budget(model: str) -> int:
    """Get active model's trim budget (respects OLLAMA_NUM_CTX)."""
    override = os.getenv("MICRO_CC_TRIM_BUDGET")
    if override:
        return int(override)
    endpoint = get_endpoint()
    if endpoint == "Ollama":
        num_ctx = int(os.getenv("OLLAMA_NUM_CTX", "32768"))
        max_output = int(os.getenv("OLLAMA_MAX_OUTPUT", "4096"))
        return compute_ollama_trim_budget(num_ctx, max_output)
    from micro_cc.models.registry import trim_budget_for
    backend = {
        "Anthropic": "anthropic",
        "Foundry": "foundry",
        "LiteLLM": "litellm",
        "OpenAI": "openai",
        "OpenRouter": "openrouter",
    }.get(endpoint)
    return trim_budget_for(model, backend=backend)
