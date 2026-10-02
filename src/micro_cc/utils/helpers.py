from dotenv import load_dotenv
from PIL import Image
import base64
import hashlib
import json
import os
import io
import sys
import warnings


############################################################################################################

load_dotenv(os.path.expanduser("~/.micro-cc/.env"))
load_dotenv()

############################################################################################################

def get_endpoint() -> str:
    """Infer endpoint from env vars. Ollama > Anthropic > Foundry > LiteLLM >
    OpenRouter > OpenAI (explicit local/proxy wins over a bare API key).

    OpenAI is deliberately last, not just unconfigured-fallback: a bare API
    key must never outrank an explicit local/proxy endpoint, and a stale key
    from a past login should not silently become the backend.
    """
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
# OPENAI_API_KEY used to be exempt from the wipe because search_tool_'s
# embeddings client shared it under every other provider. Tool discovery is
# embedding-free now, so it's an ordinary routing var: a /login to another
# provider clears it like every other provider's own keys.


def apply_login(values: dict):
    """Recreate ~/.micro-cc/.env from `values` — /login always sends the
    full field set for the chosen provider, so each login replaces the file
    instead of accumulating stale keys from a previous provider."""
    for key in _ROUTING_KEYS:
        os.environ.pop(key, None)  # clear this session's env, not just the file
    os.environ.update(values)

    env_path = os.path.expanduser("~/.micro-cc/.env")
    with open(env_path, "w") as f:
        for k, v in values.items():
            f.write(f"{k}={v}\n")


def apply_project_keys(project_dir: str, values: dict):
    """Append /keys-collected secrets to {project_dir}/.env — additive,
    unlike apply_login's full-file replace, since these accumulate across
    unrelated services instead of describing one provider config.

    Deliberately does NOT touch this process's os.environ: these are
    bash_-only secrets, sourced fresh off disk on every bash_ call (see
    tools/bash_tool.py) rather than inherited by the long-running app."""
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
    """Stable short hash for a project path — keys per-project storage dirs
    (message history under ~/.micro-cc, tool-result scratch under /tmp)
    without leaking the raw path into a folder name."""
    normalized = os.path.abspath(os.path.expanduser(project_dir))
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]

############################################################################################################
##dirs

WORK_FOLDER = os.path.join(os.getcwd(), "workspace/")

############################################################################################################


_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")


def _extract_one_leading_image_path(text: str):
    """Single-path detection — leading token of `text` if it's a quoted path
    (spaces in the filename) or a plain/backslash-escaped whitespace-delimited
    one, AND it resolves to an existing image file. Returns (remaining_text,
    path) on match, else (text, None) unchanged. Building block for
    extract_image_paths' multi-file loop below — not called directly
    elsewhere."""
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
        # Unquoted path with a backslash-escaped space (`My\ Screenshot.png`)
        # — what Terminal.app/iTerm2 actually insert for a dragged-in file
        # whose name has spaces, as opposed to wrapping the whole path in
        # quotes. A plain str.split(None, 1) would wrongly split mid-name.
        #
        # Only ASCII space/tab count as a delimiter here, not `.isspace()`'s
        # full Unicode set — macOS screenshot filenames ("Screenshot ... at
        # 12.52.53 AM.png") contain a narrow no-break space (U+202F) between
        # the time and AM/PM, which the terminal does NOT backslash-escape
        # (only literal 0x20 spaces get escaped), so `.isspace()` was
        # breaking the candidate mid-filename right before that character.
        i, n, raw = 0, len(stripped), []
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
    """Detect one or more dropped/typed image file paths leading `text` —
    what dragging file(s) in from Finder/Explorer inserts into a terminal
    (paste events are text-only; there's no such thing as raw clipboard
    image bytes reaching a TUI). Multiple dragged files land as multiple
    quoted/backslash-escaped paths back-to-back, so this consumes leading
    paths one at a time until the front of what's left isn't one. Returns
    (remaining_text, [image_paths]) — paths is [] (and remaining_text is the
    original text, untouched) when nothing at the front is an image file."""
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
    """Bound an image's actual pixel size and encoded byte size before it
    enters msgs/messages.jsonl (claude_loop_.py's tool_result_blocks) —
    the API request payload and the conversation's persisted history,
    not the TUI's own display copy (message_row_.Image resizes
    separately, purely for terminal rendering).

    Without this, a native-resolution screenshot (sanitize_and_encode_image_
    only converts format, never caps size) lands in messages.jsonl at
    several MB, and tokenization_simple._approx_tokens' char-count
    heuristic (len(json.dumps(content)) // 3) then counts that raw base64
    string as if it were dense text — one unresized screenshot can
    register as over a million "tokens", blowing the trim budget and
    triggering checkpoint-fold/summarize on what's otherwise a tiny
    conversation. Confirmed directly: an 8MB messages.jsonl with two
    3.6MB lines drove tokens.json's trimmed count to 2,691,997 in a
    17-message session.

    Uses a 2000px longer-side cap, then a quality/size step-down loop until
    under the byte budget or a floor is hit, balancing quality and file size."""
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
        # Still over budget at the lowest quality tried — shrink dimensions
        # further and take whatever that produces, rather than looping
        # indefinitely chasing an exact byte target.
        img = img.resize((img.width // 2, img.height // 2), Image.LANCZOS)
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
    except Exception:
        # Callers already filter out None (a failed image is silently
        # dropped from the batch) — no print(): this can run inside a
        # TUI's alt screen, where a bare print() lands wherever the
        # cursor happens to be instead of anywhere the user would see it.
        return None

#################################


def ollama_daemon_up(base_url: str = None) -> bool:
    """Cheap reachability probe — just "is anything answering /api/tags",
    no opinion on which models are pulled. Split out from check_ollama so
    callers can tell "daemon is down" (fixable by launching it) apart from
    "daemon is up but misconfigured" (needs a real /login fix)."""
    import urllib.request

    base_url = base_url or os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    tags_url = base_url.rstrip("/").removesuffix("/v1") + "/api/tags"
    try:
        urllib.request.urlopen(tags_url, timeout=2)
        return True
    except Exception:
        return False


def start_ollama_daemon(timeout: float = 10.0) -> bool:
    """Launch the Ollama daemon via `brew services start ollama` and poll
    until it answers or `timeout` elapses. Blocking — run off the event
    loop (e.g. asyncio.to_thread) when called from the TUI.
    """
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
    """Validate Ollama daemon reachable and model pulled.

    Non-fatal — called from inside /login while the TUI is already running,
    so it returns a message instead of printing+sys.exit like a CLI preflight
    would. Empty string means OK.
    """
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
    """Validate a model slug against OpenRouter's public /models listing,
    same non-fatal /login preflight as check_ollama. Empty string means OK."""
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


# token_cutter's max_tokens only bounds conversation history — the system
# prompt, tool schemas (tools=... on the model call), and per-loop dynamic
# context (file-changes/process-status/summary/memory-manifest) all ride on
# top of it, uncounted (see claude_loop_.py). ~2.3K tokens for the default
# tool set alone, before MCP/discovered tools or a big CLAUDE.md are added —
# 6K leaves headroom for those without eating into history budget.
OLLAMA_PROMPT_RESERVE = 6000
OLLAMA_MIN_TRIM_BUDGET = 2000


def compute_ollama_trim_budget(num_ctx: int, max_output: int) -> int:
    """Derive token_cutter's history budget from the model's real context
    window, so a small OLLAMA_NUM_CTX can't get handed a trim budget that
    overflows it — num_ctx must cover trim_budget + OLLAMA_PROMPT_RESERVE +
    max_output. Floors at OLLAMA_MIN_TRIM_BUDGET so a tiny num_ctx still
    gets a usable (if tight) budget instead of going negative."""
    return max(OLLAMA_MIN_TRIM_BUDGET, num_ctx - max_output - OLLAMA_PROMPT_RESERVE)


def effective_trim_budget(model: str) -> int:
    """Real token budget for whatever's actively running right now — same
    escape hatch and Ollama handling as start_live_._initial_trim_budget,
    exposed here so non-UI callers (e.g. msg_store_'s compaction sizing)
    can get it too without importing start_live_.

    Ollama models aren't in the registry, so trim_budget_for's
    context_window lookup would just floor to DEFAULT_TRIM_BUDGET and
    ignore the real (often much smaller) local context window — derive
    from OLLAMA_NUM_CTX/OLLAMA_MAX_OUTPUT instead. Every other endpoint
    uses the model's registered context_window via trim_budget_for.

    Must pass the resolved backend through to trim_budget_for — a bare
    trim_budget_for(model) leaves backend=None, and a free-text OpenRouter
    model (never in MODELS, so context_window lookup misses) then falls
    through to trim_budget_for's generic DEFAULT_TRIM_BUDGET (100_000)
    instead of BACKEND_DEFAULT_TRIM_BUDGET["openrouter"] (1_000_000) —
    this is what caused compaction to floor at 100k despite the 1M
    context_window declared in registry.py.
    """
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
