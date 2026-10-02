#!/bin/bash
set -euo pipefail

# ─── Colors ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Run from the script's own dir so paths (.env, env/, pyproject) resolve
# regardless of where it's invoked from.
cd "$(dirname "$0")"

# ─── Token (from project .env, not the shell) ─────────────────────────────────
# `set -a` auto-exports every var defined while sourcing; comment lines (#) and
# blank lines are ignored by the shell, so .env loads cleanly.
set -a
source .env
set +a
if [ -z "${PYPI_ORG_TOKEN:-}" ]; then
    echo -e "${RED}PYPI_ORG_TOKEN not found in .env — aborting.${NC}"
    exit 1
fi


# ─── Confirm (PyPI uploads are irreversible per version) ──────────────────────
VERSION=$(grep -E '^version *=' pyproject.toml | head -1 | cut -d'"' -f2)
IFS='.' read -r major minor patch <<< "$VERSION"
new="$major.$minor.$((patch + 1))"

# Confirm BEFORE writing — answering no must leave pyproject.toml untouched.
read -r -p "Raise $VERSION -> $new and publish? [y/N] " ok
if [[ "$ok" != y ]]; then
    echo -e "${RED}Aborted.${NC}"
    exit 1
fi

sed -i '' "s/^version *=.*/version = \"$new\"/" pyproject.toml
echo -e "${YELLOW}About to build & upload micro-cc ${new} to PyPI.${NC}"

# ─── Build the GUI ────────────────────────────────────────────────────────────
# The wheel ships compiled assets, never TSX, so users never need node — but
# publishing does. Fail loudly rather than shipping a wheel whose /gui 503s.
build_gui() {
    if ! command -v npm >/dev/null; then
        echo -e "${RED}npm not found — needed to build the GUI. Aborting.${NC}"
        exit 1
    fi
    ( cd frontend && npm ci --no-audit --no-fund && npm run build )
    if [ ! -f src/micro_cc/webui/dist/index.html ]; then
        echo -e "${RED}GUI build produced no index.html — aborting.${NC}"
        exit 1
    fi
}

# ─── Build & upload ───────────────────────────────────────────────────────────
publish() {
    rm -rf dist/ build/
    rm -rf src/micro_cc.egg-info
    source env/bin/activate
    python -m build
    twine upload dist/* -u __token__ -p "$PYPI_ORG_TOKEN"
}

# ─── Execute ─────────────────────────────────────────────────────────────────
echo -e "${GREEN}microcc publish script start — $(date '+%H:%M:%S')${NC}"
echo ""

build_gui
publish

echo ""
echo -e "${GREEN}Done — micro-cc ${new} live at https://pypi.org/project/micro-cc/${NC}"
