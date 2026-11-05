"""CI smoke: computer refuses cleanly off-mac; browser snapshots a page and acts by ref (shadow DOM, iframe, popup)."""
import asyncio
import platform
import re
import urllib.parse

from micro_cc.tools.browser_tool_ import browser, close_browser
from micro_cc.tools.computer_tool_ import computer

HTML = """<h1>ok</h1><input placeholder="Search"><button onclick="this.textContent='got '+document.querySelector('input').value">Go</button>
<div id=host></div><script>document.getElementById('host').attachShadow({mode:'open'}).innerHTML='<button>Shadow</button>'</script>
<iframe srcdoc="<button onclick=&quot;this.textContent='framed'&quot;>Inner</button>"></iframe>
<button onclick="window.open('').document.write('<h2>popped</h2>')">Pop</button>"""


def _ref(snap: str, label: str) -> str:
    return re.search(rf'"{label}"[^\n]*\[ref=(\w+)\]', snap).group(1)


async def main():
    if platform.system() != "Darwin":
        out = await computer("pass", "smoke test", project_dir=".", model="")
        assert "macOS-only" in out, out
        print("computer: refused cleanly")

    out = await browser(f"await page.goto('data:text/html,{urllib.parse.quote(HTML)}')", "smoke", project_dir=".", model="")
    assert 'heading "ok"' in out and '"Shadow"' in out and '"Inner"' in out, out
    print(re.search(r"Browser: .*", out).group(0))

    out = await browser(f"await ref('{_ref(out, 'Search')}').fill('shoes')\nawait ref('{_ref(out, 'Go')}').click()\n"
                        f"await ref('{_ref(out, 'Inner')}').click()", "smoke", project_dir=".", model="")
    assert '"got shoes"' in out and '"framed"' in out, out
    print("browser: snapshot + ref actions (shadow, iframe) ok")

    shot = await browser("", "smoke", project_dir=".", model="", screenshot=True)
    assert shot["type"] == "image" and shot["data"] and "Snapshot:" in shot["text"], shot.keys()
    print("browser: screenshot attached with snapshot")

    out = await browser(f"await ref('{_ref(out, 'Pop')}').click()", "smoke", project_dir=".", model="")
    assert 'heading "popped"' in out, out
    print("browser: popup adopted")
    await close_browser()


asyncio.run(main())
