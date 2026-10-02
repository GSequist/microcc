"""
Skill Loader - Parses YAML frontmatter from SKILL.md files and provides skill discovery.

Skills are loaded from three places and merged, each tier overriding the last
on name collision:
1. Built-in: {this package}/skills/*/SKILL.md
2. Global (user-level): ~/.micro-cc/skills/*/SKILL.md — applies across ALL
   projects, same home as memory.json. For skills a user wants everywhere
   regardless of which project_dir micro-cc is pointed at.
3. Project-local: {project_dir}/skills/*/SKILL.md — lets a user drop a
   skills/ folder at the root of whatever project they point micro-cc at,
   scoped to just that project (e.g. checked into that project's own repo).

All three use the exact same SKILL.md format as the built-ins.

Progressive disclosure pattern:
1. Level 1: Skill metadata (name+description) injected into system prompt
2. Level 2: Agent calls read_skill() to load full SKILL.md content
3. Level 3: Agent reads reference files or executes scripts via kernel
"""

import re
from pathlib import Path
from typing import Optional

# Cache keyed by project_dir ("" for builtin+global only)
_skills_cache_by_dir: dict[str, dict] = {}

SKILLS_DIR = Path(__file__).parent
GLOBAL_SKILLS_DIR = Path.home() / ".micro-cc" / "skills"


def _parse_yaml_frontmatter(content: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from markdown content.

    Returns:
        tuple of (frontmatter_dict, remaining_content)
    """
    frontmatter = {}
    body = content

    # Match YAML frontmatter between --- markers
    pattern = r'^---\s*\n(.*?)\n---\s*\n(.*)$'
    match = re.match(pattern, content, re.DOTALL)

    if match:
        yaml_content = match.group(1)
        body = match.group(2)

        # Simple YAML parsing (name: value pairs)
        for line in yaml_content.split('\n'):
            line = line.strip()
            if ':' in line:
                key, value = line.split(':', 1)
                key = key.strip()
                value = value.strip()
                # Remove quotes if present
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                elif value.startswith("'") and value.endswith("'"):
                    value = value[1:-1]
                frontmatter[key] = value

    return frontmatter, body


def _scan_skills_dir(skills_dir: Path) -> dict:
    """Scan a skills/ folder and return {name: skill_entry} for every SKILL.md found."""
    found = {}

    if not skills_dir.is_dir():
        return found

    for skill_dir in skills_dir.iterdir():
        if not skill_dir.is_dir():
            continue

        skill_file = skill_dir / 'SKILL.md'
        if not skill_file.exists():
            continue

        try:
            content = skill_file.read_text(encoding='utf-8')
            frontmatter, body = _parse_yaml_frontmatter(content)

            name = frontmatter.get('name', skill_dir.name)
            description = frontmatter.get('description', '')

            found[name] = {
                'name': name,
                'description': description,
                'path': str(skill_dir),
                'skill_file': str(skill_file),
                'full_content': content,
                'body': body,
            }
        except Exception as e:
            print(f"Error loading skill from {skill_dir}: {e}")

    return found


def _load_skills(project_dir: str = "") -> dict:
    """Load built-in + global (~/.micro-cc/skills/) + project-local skills/, merged.

    Later tiers override earlier ones on name collision:
    built-in -> global (all projects) -> project-local (this project_dir only).
    Cached per project_dir so repeated calls within the same project are free.
    """
    cache_key = project_dir or ""
    if cache_key in _skills_cache_by_dir:
        return _skills_cache_by_dir[cache_key]

    skills = _scan_skills_dir(SKILLS_DIR)
    skills.update(_scan_skills_dir(GLOBAL_SKILLS_DIR))

    if project_dir:
        # Project-local skills override built-in/global with the same name.
        skills.update(_scan_skills_dir(Path(project_dir) / "skills"))

    _skills_cache_by_dir[cache_key] = skills
    return skills


def get_available_skills(project_dir: str = "") -> list[dict]:
    """Get list of available skills with name and description.

    Returns:
        List of dicts with 'name' and 'description' keys.
    """
    skills = _load_skills(project_dir)
    return [
        {'name': skill['name'], 'description': skill['description']}
        for skill in skills.values()
    ]


def get_skill_summary(project_dir: str = "") -> str:
    """Get formatted summary of all skills for injection into system prompt.

    Returns:
        Formatted string listing all available skills.
    """
    skills = _load_skills(project_dir)

    if not skills:
        return ""

    lines = ["Available skills (use `read_skill` tool to load full instructions):"]
    for skill in skills.values():
        lines.append(f"- **{skill['name']}**: {skill['description']}")

    return "\n".join(lines)


def get_skill_content(skill_name: str, project_dir: str = "") -> Optional[str]:
    """Get the full content of a skill's SKILL.md file.

    Args:
        skill_name: Name of the skill to load.

    Returns:
        Full SKILL.md content or None if skill not found.
    """
    skills = _load_skills(project_dir)

    skill = skills.get(skill_name)
    if skill:
        return skill['body']  # Return body without frontmatter
    return None


def get_skill_path(skill_name: str, project_dir: str = "") -> Optional[str]:
    """Get the filesystem path to a skill's folder.

    Args:
        skill_name: Name of the skill.

    Returns:
        Path to skill folder or None if not found.
    """
    skills = _load_skills(project_dir)

    skill = skills.get(skill_name)
    if skill:
        return skill['path']
    return None


def list_skill_files(skill_name: str, project_dir: str = "") -> list[str]:
    """List all files in a skill's folder.

    Args:
        skill_name: Name of the skill.

    Returns:
        List of relative file paths within the skill folder.
    """
    skills = _load_skills(project_dir)

    skill = skills.get(skill_name)
    if not skill:
        return []

    skill_path = Path(skill['path'])
    files = []

    for item in skill_path.rglob('*'):
        if item.is_file():
            files.append(str(item.relative_to(skill_path)))

    return files


def reload_skills(project_dir: str = ""):
    """Force reload of the skills cache for project_dir. Useful after adding new skills."""
    _skills_cache_by_dir.pop(project_dir or "", None)
    _load_skills(project_dir)
