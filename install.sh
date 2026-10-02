#!/bin/sh
# Installs micro-cc on macOS, Linux and WSL: uv (which brings its own Python), then micro-cc as a uv tool.
set -eu

case "$(uname -s)" in
    Darwin|Linux) ;;
    *) echo "micro-cc needs macOS, Linux or WSL (run install.ps1 on Windows)." >&2; exit 1 ;;
esac

PREV="$(command -v microcc 2>/dev/null || true)"

if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi

uv tool install --python 3.12 --force micro-cc
uv tool update-shell >/dev/null 2>&1 || true

NEW="$(uv tool dir --bin)/microcc"
if [ -n "$PREV" ] && [ "$PREV" != "$NEW" ]; then
    echo ""
    echo "Found an older install at $PREV. It can shadow the new one; remove it with:"
    echo "  pip uninstall micro-cc    (or: pipx uninstall micro-cc)"
fi

echo ""
echo "micro-cc installed. Open a new terminal, then run: microcc /path/to/project"
