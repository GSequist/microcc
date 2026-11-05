"""Status line script resolution: user ~/.micro-cc/statusline.sh wins, else the builtin default."""

import os

from micro_cc import mods_
from micro_cc.utils.default_status_sh import _DEFAULT_STATUS_SCRIPT
from micro_cc.utils.legacy_status_sh import _LEGACY_STATUS_SCRIPT


def user_script() -> str:
    return str(mods_.user_dir() / "statusline.sh")


def default_script() -> str:
    return str(mods_.user_dir() / "cache" / "statusline.default.sh")


def migrate() -> bool:
    """Delete user statusline.sh if it is an unedited copy of the current or the previous (Unicode/Nerd) default."""
    path = user_script()
    try:
        with open(path) as f:
            same = f.read() in (_DEFAULT_STATUS_SCRIPT, _LEGACY_STATUS_SCRIPT)
        if same:
            os.remove(path)
        return same
    except OSError:
        return False


def _write_default() -> str:
    path = default_script()
    try:
        with open(path) as f:
            if f.read() == _DEFAULT_STATUS_SCRIPT and os.access(path, os.X_OK):
                return path
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(_DEFAULT_STATUS_SCRIPT)
    os.chmod(path, 0o755)
    return path


def resolve() -> str:
    """Path to script: user's file if present, else harness-owned default."""
    path = user_script()
    return path if os.path.exists(path) else _write_default()
