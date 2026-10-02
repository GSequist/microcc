from micro_cc.browser.simpletextbrowser import SimpleTextBrowser
from dotenv import load_dotenv
from micro_cc.utils.helpers import WORK_FOLDER
import time
import os

load_dotenv()

BROWSER_TTL_SECONDS = 1800

class BrowserManager:
    def __init__(self):
        self.browsers = {}

    def get_browser(self, project_dir):
        self._cleanup_stale()
        if project_dir not in self.browsers:
            default_request_kwargs = {
                "timeout": (10, 10),
                "headers": {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0 Safari/537.36"
                    )
                },
            }
            self.browsers[project_dir] = SimpleTextBrowser(
                start_page="about:blank",
                viewport_size=1024 * 8,
                downloads_folder=os.path.join(WORK_FOLDER, project_dir),
                serpapi_key=os.getenv("SERPAPI_KEY"),
                request_kwargs=default_request_kwargs,
                project_dir=project_dir,
            )
        return self.browsers[project_dir]

    def _cleanup_stale(self):
        """Remove browsers idle longer than TTL"""
        now = time.time()
        stale = [
            uid
            for uid, browser in self.browsers.items()
            if browser.history and (now - browser.history[-1][1]) > BROWSER_TTL_SECONDS
        ]
        for uid in stale:
            del self.browsers[uid]
