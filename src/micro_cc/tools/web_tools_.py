from micro_cc.browser.browser_manager import BrowserManager
from dotenv import load_dotenv
import requests

load_dotenv()

browser_manager = BrowserManager()


def google_search(query: str, filter_year: int = None, *, project_dir) -> str:
    """Search the web and return the results page text.

    Args:
        query: Text query to search for
        filter_year: Ignored; no year filter is applied
    """
    browser = browser_manager.get_browser(project_dir)
    browser.visit_page(f"google: {query}", filter_year=None)
    header, content = browser._state()
    return header.strip() + "\n=======================\n" + content


def visit_url(url: str, *, project_dir) -> str:
    """Visit a URL and return its text. YouTube URLs return the transcript; file URLs are downloaded for text_file.

    Args:
        url: The URL to visit
    """
    browser = browser_manager.get_browser(project_dir)
    browser.visit_page(url)
    header, content = browser._state()
    return header.strip() + "\n=======================\n" + content


def archive_search(url: str, date: str, *, project_dir) -> str:
    """Return the Wayback Machine snapshot closest to date.

    Args:
        url: The URL to find in archive
        date: Target date as YYYYMMDD
    """
    browser = browser_manager.get_browser(project_dir)
    base_api = f"https://archive.org/wayback/available?url={url}"
    archive_api = base_api + f"&timestamp={date}"
    res_with_ts = requests.get(archive_api).json()
    res_without_ts = requests.get(base_api).json()

    if (
        "archived_snapshots" in res_with_ts
        and "closest" in res_with_ts["archived_snapshots"]
    ):
        closest = res_with_ts["archived_snapshots"]["closest"]
    elif (
        "archived_snapshots" in res_without_ts
        and "closest" in res_without_ts["archived_snapshots"]
    ):
        closest = res_without_ts["archived_snapshots"]["closest"]
    else:
        return f"Archive not found for {url}."

    target_url = closest["url"]
    browser.visit_page(target_url)
    header, content = browser._state()
    return (
        f"web archive for url {url}, snapshot on {closest['timestamp'][:8]}:\n"
        + header.strip()
        + "\n=======================\n"
        + content
    )


def page_up(project_dir) -> str:
    """Scroll up one page."""
    browser = browser_manager.get_browser(project_dir)
    browser.page_up()
    header, content = browser._state()
    return header.strip() + "\n=======================\n" + content


def page_down(project_dir) -> str:
    """Scroll down one page."""
    browser = browser_manager.get_browser(project_dir)
    browser.page_down()
    header, content = browser._state()
    return header.strip() + "\n=======================\n" + content


def find_on_page(search_string: str, *, project_dir) -> str:
    """Scroll to the first occurrence of search_string (Ctrl+F).

    Args:
        search_string: Text to find; supports wildcards like '*'
    """
    browser = browser_manager.get_browser(project_dir)
    result = browser.find_on_page(search_string)
    header, content = browser._state()

    if result is None:
        return header.strip() + f"\n=======================\nThe search string '{search_string}' was not found on this page."

    return header.strip() + "\n=======================\n" + content


def find_next(project_dir) -> str:
    """Find next occurrence of previous search."""
    browser = browser_manager.get_browser(project_dir)
    result = browser.find_next()
    header, content = browser._state()

    if result is None:
        return header.strip() + "\n=======================\nNo further occurrences found."

    return header.strip() + "\n=======================\n" + content


def download_from_url(url: str, *, project_dir) -> str:
    """Download a file (xlsx, pptx, docx, wav, mp3, pdf, etc.) for text_file.

    Args:
        url: URL of the file to download
    """
    browser = browser_manager.get_browser(project_dir)
    browser.visit_page(url)
    header, content = browser._state()
    return header.strip() + "\n=======================\n" + content


def text_file(filename: str, *, project_dir) -> str:
    """Convert a downloaded file to text/markdown.

    Args:
        filename: Name of a previously downloaded file
    """
    browser = browser_manager.get_browser(project_dir)
    content = browser.read_downloaded_file(filename)
    return content if content else f"Could not read file: {filename}"
