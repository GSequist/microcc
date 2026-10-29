"""Sizes an OptionPanel to the rows free above the bottom bar."""

from micro_cc.tui_native import option_panel_

PANEL_CHROME = 2  # footer hint row, plus the scroll count it may share
MIN_TITLE_LINES = 2


def fit(app, items: list, title: str = "") -> tuple[int, int, int | None]:
    """(max_visible, max_desc_lines, max_title_lines): options always fit; the title is trimmed first."""
    width, height = app.tui._terminal_size()
    reserved = (
        len(app.banner.render(width))
        + len(app.working.render(width))
        + len(app.ask_question_text.render(width))
        + len(app.memory_flash.render(width))
        + 2  # top_rule + bottom_rule
        + len(app.statusbar.render(width))
        + len(app.static_hintbar.render(width))
        + len(app.subagent_status.render(width))
        + len(app.bgproc_status.render(width))
        + PANEL_CHROME
    )
    num_items = max(1, len(items))
    free = max(1, height - reserved)
    title_lines = sum(len(option_panel_._wrap_text(p, max(1, width - 2))) for p in title.split("\n")) if title else 0
    title_cap = min(title_lines, max(MIN_TITLE_LINES, free - num_items)) if title_lines else None
    budget = max(1, free - (title_cap or 0))
    if not any(it.description for it in items):
        return budget, option_panel_.MAX_DESC_LINES, title_cap
    if budget < num_items:
        return budget, 1, title_cap  # no room for descriptions: scroll one line per item
    return num_items, max(1, budget // num_items - 1), title_cap  # -1 reserves the label line
