# micro-cc

```
                   88
                   ""

88,dPYba,,adPYba,  88  ,adPPYba, 8b,dPPYba,  ,adPPYba,     ,adPPYba,  ,adPPYba,
88P'   "88"    "8a 88 a8"     "" 88P'   "Y8 a8"     "8a   a8"     "" a8"     ""
88      88      88 88 8b         88         8b       d8 · 8b         8b
88      88      88 88 "8a,   ,aa 88         "8a,   ,a8"   "8a,   ,aa "8a,   ,aa
88      88      88 88  `"Ybbd8"' 88          `"YbbdP"'     `"Ybbd8"'  `"Ybbd8"'
Knowledge work is, by extension, a coding problem.

enter submit · esc interrupt · / commands
```

A terminal harness that gives frontier models full system access: shell, filesystem, browser and MCP.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/GSequist/microcc/main/install.sh | sh
```

macOS, Linux and WSL. On Windows, run this in PowerShell (it installs inside WSL):

```powershell
irm https://raw.githubusercontent.com/GSequist/microcc/main/install.ps1 | iex
```

Or `pip install micro-cc`.

## Use

```bash
microcc /path/to/project
```

Type `/login` on first run, `/` for commands. `microcc-headless` runs a single prompt non-interactively.

## License

MIT. Third-party attributions in [THIRD_PARTY_NOTICES.md](https://github.com/GSequist/microcc/blob/main/THIRD_PARTY_NOTICES.md).
