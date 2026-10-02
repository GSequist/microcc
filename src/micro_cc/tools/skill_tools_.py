"""Skill Tools - Tools for reading and using agent skills."""

from micro_cc.skills.skill_loader import (
    get_skill_content,
    get_skill_path,
    list_skill_files,
    get_available_skills,
)
from pathlib import Path


def read_skill(skill_name: str, *, project_dir) -> str:
    """Load skill instructions. Supports main skills and subskills.

    Args:
        skill_name: Skill path. Examples:
            - "docx" - loads main SKILL.md
            - "docx/ooxml.md" - loads subskill
    """
    # Check if requesting a subskill
    if "/" in skill_name or skill_name.endswith(".md"):
        return _read_subskill(skill_name, project_dir)

    # Main skill - load SKILL.md
    content = get_skill_content(skill_name, project_dir)

    if not content:
        available = get_available_skills(project_dir)
        skill_names = [s["name"] for s in available]
        return f"Skill '{skill_name}' not found. Available: {', '.join(skill_names)}"

    skill_path = get_skill_path(skill_name, project_dir)
    files = list_skill_files(skill_name, project_dir)
    md_files = [f for f in files if f.endswith(".md") and f != "SKILL.md"]
    # Everything else (scripts/*.py, .js, .sh, ...) is real code already on
    # disk — not something to load into context, something to run. This used
    # to be silently dropped here even though list_skill_files (above)
    # already enumerated it: a skill whose SKILL.md didn't happen to narrate
    # its own script filenames in prose left them completely undiscoverable,
    # since nothing told the model they existed or where. Surfacing them
    # with their absolute path makes them directly runnable via bash_/shell
    # from any cwd, regardless of whether the prose mentions them.
    # __pycache__/*.pyc (or any other dotfile/dir noise) is bytecode Python
    # itself leaves behind after a script runs — never source, never
    # something to hand the model as if it were runnable.
    script_files = [
        f for f in files
        if not f.endswith(".md")
        and "__pycache__" not in Path(f).parts
        and not f.endswith(".pyc")
    ]

    response_parts = [
        f"# Skill: {skill_name}",
        "",
        content,
        "",
        "---",
        f"**Skill location**: {skill_path}",
    ]

    if md_files:
        response_parts.append("**Related documentation** (use read_skill to load):")
        for f in md_files:
            response_parts.append(f"  - {skill_name}/{f}")

    if script_files:
        response_parts.append("**Scripts** (run directly, e.g. `python <path> ...`):")
        for f in script_files:
            response_parts.append(f"  - {skill_path}/{f}")

    return "\n".join(response_parts)


def _read_subskill(skill_path: str, project_dir: str) -> str:
    """Read a subskill file within a skill folder."""
    parts = skill_path.split("/", 1)
    if len(parts) == 1:
        base_skill = parts[0].replace(".md", "")
        subfile = parts[0]
    else:
        base_skill = parts[0]
        subfile = parts[1]

    base_path = get_skill_path(base_skill, project_dir)
    if not base_path:
        available = get_available_skills(project_dir)
        skill_names = [s["name"] for s in available]
        return f"Skill '{base_skill}' not found. Available: {', '.join(skill_names)}"

    full_path = Path(base_path) / subfile
    if not full_path.exists():
        files = list_skill_files(base_skill, project_dir)
        md_files = [f for f in files if f.endswith(".md")]
        return f"Subskill '{subfile}' not found in {base_skill}. Available: {', '.join(md_files)}"

    content = full_path.read_text(encoding="utf-8")
    return f"# {base_skill}/{subfile}\n\n{content}"


def list_skills(*, project_dir) -> str:
    """List all available skills with their descriptions."""
    skills = get_available_skills(project_dir)

    if not skills:
        return "No skills available."

    lines = ["# Available Skills", ""]
    for skill in skills:
        lines.append(f"**{skill['name']}**")
        lines.append(f"{skill['description']}")
        lines.append("")

    return "\n".join(lines)
