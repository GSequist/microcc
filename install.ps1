# micro-cc's bash_ tool needs a POSIX shell, so on Windows this installs inside WSL.
$ErrorActionPreference = "Stop"

if (-not (Get-Command wsl -ErrorAction SilentlyContinue)) {
    Write-Host "WSL is required. Run 'wsl --install' in an admin PowerShell, reboot, then run this again."
    exit 1
}

wsl -e sh -c "curl -fsSL https://raw.githubusercontent.com/GSequist/micro-cc/main/install.sh | sh"
Write-Host "Start it with: wsl microcc /path/to/project"
