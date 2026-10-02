#!/usr/bin/env bash
# Installs whatever this skill needs and doesn't already have on the machine:
# Node.js, ffmpeg, the npm `playwright` package, and a real Chrome for it to drive.
# Idempotent — safe to re-run, only installs what's missing. Nothing here touches
# micro-cc's own pyproject.toml/pip dependencies; this is all shelled-out system tooling.
#
# Run from the target project directory (the one with package.json).
set -euo pipefail

need_brew() {
  if ! command -v brew >/dev/null 2>&1; then
    echo "Homebrew not found. Install it first: https://brew.sh" >&2
    exit 1
  fi
}

echo "== node/npm =="
if ! command -v node >/dev/null 2>&1; then
  case "$(uname -s)" in
    Darwin) need_brew; brew install node ;;
    Linux)  sudo apt-get update && sudo apt-get install -y nodejs npm ;;
    *) echo "Unsupported OS for auto-install; install Node.js manually." >&2; exit 1 ;;
  esac
else
  echo "node $(node -v) already present"
fi

echo "== ffmpeg =="
if ! command -v ffmpeg >/dev/null 2>&1; then
  case "$(uname -s)" in
    Darwin) need_brew; brew install ffmpeg ;;
    Linux)  sudo apt-get update && sudo apt-get install -y ffmpeg ;;
    *) echo "Unsupported OS for auto-install; install ffmpeg manually." >&2; exit 1 ;;
  esac
else
  echo "$(ffmpeg -version | head -1) already present"
fi

echo "== npm playwright + Chrome =="
if [ ! -d node_modules/playwright ]; then
  npm install playwright
else
  echo "npm playwright already present"
fi
npx playwright install chrome

echo "stack ready."
