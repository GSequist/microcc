"""Sizes an OptionPanel to the rows free above the bottom bar."""

from micro_cc.tui_native import option_panel_

PANEL_CHROME = 2  # footer hint row, plus the scroll count it may share


def fit(app, items: list, title: str = "") -> tuple[int, int]:
    """(max_visible, max_desc_lines) that fit the rows left above the bottom bar."""
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
        + (len(option_panel_._wrap_text(title, max(1, width - 6))) if title else 0)
    )
    budget = max(1, height - reserved)
    num_items = max(1, len(items))
    if not any(it.description for it in items):
        return budget, option_panel_.MAX_DESC_LINES
    if budget < num_items:
        return budget, 1  # no room for descriptions: scroll one line per item
    return num_items, max(1, budget // num_items - 1)  # -1 reserves the label line
