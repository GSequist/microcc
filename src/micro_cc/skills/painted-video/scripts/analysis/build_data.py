# Vendored from github.com/ledbetterljoshua/functional-emotions-video (MIT — see ../../LICENSE-lib).
# Bundles lyric timing + audio features into js/data.js (loaded as a plain script so the page works
# from file://). Run last, after features.py -> transcribe.py -> align.py. This OVERWRITES the
# scaffold.py-generated manual-beat-grid js/data.js with real vocal/lyric-synced timing.
import json
L = json.load(open("analysis/lyrics_timed.json")); F = json.load(open("analysis/features.json"))
lines = []
for i, t in enumerate(L["lines"]):
    ws = [dict(t=w["t"], s=w["s"], e=w["e"]) for w in L["words"] if w["line"] == i]
    lines.append(dict(text=t, s=ws[0]["s"], e=ws[-1]["e"], words=ws))
feat = {k: F[k] for k in ["rms", "low", "mid", "high", "onset", "perc", "cent"]}
out = "window.LYRICS=" + json.dumps(lines, separators=(',', ':')) + ";\nwindow.FEAT=" + json.dumps(dict(fps=F["fps"], beats=F["beats"], **feat), separators=(',', ':')) + ";\n"
open("js/data.js", "w").write(out)
