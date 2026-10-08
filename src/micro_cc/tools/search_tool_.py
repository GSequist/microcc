from micro_cc.models.schema import function_to_schema
from functools import partial
from pathlib import Path
from typing import Optional, TYPE_CHECKING
import json
import platform

from micro_cc.tools.mcp_client_ import resolve_mcp_tools

# Import all discoverable tools
from micro_cc.tools.browser_tool_ import browser
from micro_cc.tools.computer_tool_ import computer
from micro_cc.tools.message_session_ import message_session_
from micro_cc.tools.reload_tool_ import reload_harness_
from micro_cc.tools.web_tools_ import (
    visit_url,
    google_search,
    archive_search,
    page_up,
    page_down,
    find_on_page,
    find_next,
    download_from_url,
    text_file,
)
from dotenv import load_dotenv
import os

load_dotenv(os.path.expanduser("~/.micro-cc/.env"))
load_dotenv()

TOOL_CATALOG = {}
MCP_RESOLVED = {}


def _register(func, search_text: str):
    name = func.func.__name__ if isinstance(func, partial) else func.__name__
    TOOL_CATALOG[name] = {
        "func": func,
        "schema": function_to_schema(func),
        "search_text": f"{name} {search_text}",
    }


_register(message_session_, "message notify tell list find discover other running micro-cc sessions peers terminal cross-session communicate")
_register(reload_harness_, "reload restart relaunch harness self apply changes edited source mods mod modification")
_register(browser, "browser chrome playwright navigate click type web automation interact page form")
# Screenshot capture and AX scoping are mac-only: keep the tool out of the catalog elsewhere.
if platform.system() == "Darwin":
    _register(computer, "computer desktop mac mouse keyboard click type screenshot control pyautogui")
_register(
    visit_url,
    "visit any url to receive back its content in markdown",
)
_register(
    google_search,
    "web search google internet query find information online browse",
)
_register(
    archive_search,
    "wayback machine archive historical web page past version internet archive",
)
_register(
    page_up,
    "scroll up page navigation browser view previous content",
)
_register(
    page_down,
    "scroll down page navigation browser view more content continue reading",
)
_register(
    find_on_page,
    "find search text on page ctrl+f locate string browser",
)
_register(
    find_next,
    "find next occurrence search continue browser navigation",
)
_register(
    download_from_url,
    "download file url xlsx pptx docx wav mp3 save file from web",
)
_register(
    text_file,
    "read downloaded file convert to text markdown xlsx pptx docx pdf content",
)

MCP_CATALOG = {
    "manifold": {
        "server": {
            "type": "url",
            "url": "https://api.manifold.markets/v0/mcp",
            "name": "manifold",
        },
        "search_text": "prediction markets forecasting sentiment trends betting odds probability",
    },
    "deepwiki": {
        "server": {
            "type": "url",
            "url": "https://mcp.deepwiki.com/mcp",
            "name": "deepwiki",
        },
        "search_text": "github repository documentation wiki architecture explanation codebase understanding",
    },
}

GLOBAL_MCPS_DIR = Path.home() / ".micro-cc" / "mcps"
_global_mcp_cache: Optional[dict] = None
_project_mcp_cache: dict[str, dict] = {}


def _scan_mcps_dir(mcps_dir: Path) -> dict:
    catalog = {}
    if not mcps_dir.is_dir():
        return catalog

    for entry in mcps_dir.iterdir():
        config_file = entry / "mcp.json" if entry.is_dir() else entry
        if config_file.suffix != ".json" or not config_file.exists():
            continue
        try:
            data = json.loads(config_file.read_text(encoding="utf-8"))
            server = data["server"]
            name = server.get("name", entry.stem)
            catalog[name] = {
                "server": server,
                "search_text": data.get("search_text", name),
                "auth_env": data.get("auth_env"),
            }
        except Exception:
            continue

    return catalog


def _load_global_mcps() -> dict:
    global _global_mcp_cache
    if _global_mcp_cache is None:
        _global_mcp_cache = _scan_mcps_dir(GLOBAL_MCPS_DIR)
    return _global_mcp_cache


def _load_project_mcps(project_dir: str) -> dict:
    if not project_dir:
        return {}
    if project_dir not in _project_mcp_cache:
        _project_mcp_cache[project_dir] = _scan_mcps_dir(Path(project_dir) / "mcps")
    return _project_mcp_cache[project_dir]


def get_effective_mcp_catalog(project_dir: str = "") -> dict:
    merged = dict(MCP_CATALOG)
    merged.update(_load_global_mcps())
    merged.update(_load_project_mcps(project_dir))
    return merged


def list_mcps(*, project_dir: str = "") -> str:
    catalog = get_effective_mcp_catalog(project_dir)

    if not catalog:
        return "No MCP servers available."

    lines = ["# Available MCP servers", ""]
    for name, entry in catalog.items():
        url = entry["server"].get("url", "")
        lines.append(f"**{name}** — {entry['search_text']}")
        lines.append(f"  {url}")
        lines.append("")

    return "\n".join(lines)


def _summary(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def discover(project_dir: str = "") -> str:
    lines = ["# Tools you can add (use search_tools(action=\"add\", names=\"...\"), then call via use_tool_)", ""]
    for name, data in TOOL_CATALOG.items():
        desc = _summary(data["schema"].get("description", ""))
        lines.append(f"- {name} — {desc}")

    mcp_catalog = get_effective_mcp_catalog(project_dir)
    if mcp_catalog:
        lines += ["", "# MCP servers you can add", ""]
        for name, data in mcp_catalog.items():
            lines.append(f"- {name} — {data['search_text']}")

    return "\n".join(lines)


def parse_names(names: str) -> list[str]:
    return [n.strip() for n in (names or "").replace("\n", ",").split(",") if n.strip()]


def schema_text(schema: dict) -> str:
    return json.dumps(schema, separators=(",", ":"), sort_keys=True)


def find_tool_schema(name: str):
    if name in TOOL_CATALOG:
        return TOOL_CATALOG[name]["schema"]
    for resolved in MCP_RESOLVED.values():
        for schema in resolved["tools"]:
            if schema["name"] == name:
                return schema
    return None


async def search_tools(
    action: str,
    names: str = "",
    *,
    project_dir: str = "",
):
    """Discover and add tools. Two actions:

    action="discover" — list every tool and MCP server that can be added,
        with a one-line description each. No schemas, so it's cheap. Call
        this first to see your portfolio.

    action="add" — load the named tools/MCPs. Pass one or more names,
        comma-separated, exactly as they appear in the discover listing. The
        result contains each tool's schema as text; call the tool with
        use_tool_(name, args) matching that schema.

    Args:
        action: "discover" or "add".
        names: comma-separated tool/MCP names, e.g. "browser, deepwiki".
            Required for action="add", ignored for action="discover".
    """
    mcp_catalog = get_effective_mcp_catalog(project_dir)

    if action == "discover":
        return discover(project_dir)

    if action != "add":
        return f"Unknown action {action!r} — use \"discover\" or \"add\"."

    wanted = parse_names(names)
    if not wanted:
        return "action=\"add\" needs names=\"...\" — call action=\"discover\" first to see what's available."

    unknown = [n for n in wanted if n not in TOOL_CATALOG and n not in mcp_catalog]
    if unknown:
        return (
            f"Unknown tool/MCP name(s): {', '.join(unknown)}. "
            "Call search_tools(action=\"discover\") for the exact names."
        )

    sections = []
    for name in wanted:
        if name in TOOL_CATALOG:
            sections.append(f"## {name}\n{schema_text(TOOL_CATALOG[name]['schema'])}")
            continue
        mcp_tools, routing, failures = await resolve_mcp_tools([mcp_catalog[name]])
        if failures:
            sections.append(f"## {name}\n" + "\n".join(failures))
            continue
        MCP_RESOLVED[name] = {"tools": mcp_tools, "routing": routing}
        body = "\n".join(f"### {t['name']}\n{schema_text(t)}" for t in mcp_tools)
        sections.append(f"## {name} (MCP server)\n{body or 'no tools exposed'}")

    return (
        "Loaded. Call each tool with use_tool_(name=\"<tool name>\", args={...}) "
        "matching its schema below.\n\n" + "\n\n".join(sections)
    )


def get_tool_schema(name: str):
    return TOOL_CATALOG[name]["schema"]


def get_tool_func(name: str):
    return TOOL_CATALOG[name]["func"]
