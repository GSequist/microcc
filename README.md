# micro-cc

A terminal harness that gives frontier models full system access: shell, filesystem, browser and MCP.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/GSequist/micro-cc/main/install.sh | sh
```

macOS, Linux and WSL (Windows: `irm https://raw.githubusercontent.com/GSequist/micro-cc/main/install.ps1 | iex`, which installs inside WSL). Or `pip install micro-cc`.

## Use

```bash
microcc /path/to/project
```

Type `/login` on first run, `/` for commands. `microcc-headless` runs a single prompt non-interactively.

## License

MIT. Third-party attributions in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
