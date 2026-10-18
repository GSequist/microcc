"""Skill discovery: SKILL.md frontmatter from built-in, global (~/.micro-cc/skills) and project-local skills/.

Later tiers override earlier ones on name collision. Name and description go in the system prompt; the body loads on demand.
"""

import re
from pathlib import Path
from typing import Optional

# Cache keyed by project_dir ("" for builtin+global only)
_skills_cache_by_dir: dict[str, dict] = {}

SKILLS_DIR = Path(__file__).parent
GLOBAL_SKILLS_DIR = Path.home() / ".micro-cc" / "skills"


def _parse_yaml_frontmatter(content: str) -> tuple[dict, str]:
    """Return (frontmatter dict, body) from a ---delimited YAML header."""
    frontmatter = {}
    body = content

    # Match YAML frontmatter between --- markers
    pattern = r'^---\s*\n(.*?)\n---\s*\n(.*)$'
    match = re.match(pattern, content, re.DOTALL)

    if match:
        yaml_content = match.group(1)
        body = match.group(2)

        # Flat key: value pairs only, not full YAML.
        for line in yaml_content.split('\n'):
            line = line.strip()
            if ':' in line:
                key, value = line.split(':', 1)
                key = key.strip()
                value = value.strip()
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
    """Merge built-in, global, then project-local skills (later wins); cached per project_dir."""
    cache_key = project_dir or ""
    if cache_key in _skills_cache_by_dir:
        return _skills_cache_by_dir[cache_key]

    skills = _scan_skills_dir(SKILLS_DIR)
    skills.update(_scan_skills_dir(GLOBAL_SKILLS_DIR))

    if project_dir:
        skills.update(_scan_skills_dir(Path(project_dir) / "skills"))

    _skills_cache_by_dir[cache_key] = skills
    return skills


def get_available_skills(project_dir: str = "") -> list[dict]:
    """Available skills as [{name, description}]."""
    skills = _load_skills(project_dir)
    return [
        {'name': skill['name'], 'description': skill['description']}
        for skill in skills.values()
    ]


def get_skill_summary(project_dir: str = "") -> str:
    """Skill list formatted for the system prompt."""
    skills = _load_skills(project_dir)

    if not skills:
        return ""

    lines = ["Available skills (use `read_skill` tool to load full instructions):"]
    for skill in skills.values():
        lines.append(f"- **{skill['name']}**: {skill['description']}")

    return "\n".join(lines)


def get_skill_content(skill_name: str, project_dir: str = "") -> Optional[str]:
    """Body of a skill's SKILL.md (frontmatter stripped), or None."""
    skills = _load_skills(project_dir)

    skill = skills.get(skill_name)
    if skill:
        return skill['body']
    return None


def get_skill_path(skill_name: str, project_dir: str = "") -> Optional[str]:
    """Filesystem path of a skill's folder, or None."""
    skills = _load_skills(project_dir)

    skill = skills.get(skill_name)
    if skill:
        return skill['path']
    return None


def list_skill_files(skill_name: str, project_dir: str = "") -> list[str]:
    """Relative paths of all files in a skill's folder."""
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
