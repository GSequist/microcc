import os
def load_claude_md_file(project_dir: str) -> str:
    """Load CLAUDE.md from project directory if it exists."""
    claude_md_path = os.path.join(project_dir, "CLAUDE.md")
    if os.path.exists(claude_md_path):
        with open(claude_md_path, "r") as f:
            return f.read()
    return ""
