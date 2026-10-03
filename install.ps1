# micro-cc's bash_ tool needs a POSIX shell, so on Windows this installs inside WSL.
$ErrorActionPreference = "Stop"

if (-not (Get-Command wsl -ErrorAction SilentlyContinue)) {
    Write-Host "WSL is required. Run 'wsl --install' in an admin PowerShell, reboot, then run this again."
    exit 1
}

# wsl.exe exists on machines with no distro installed; make sure one actually runs.
wsl -e true 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "WSL has no working Linux distro. Run 'wsl --install' in an admin PowerShell, reboot, then run this again."
    exit 1
}

wsl -e sh -c "curl -fsSL https://raw.githubusercontent.com/GSequist/micro-cc/main/install.sh | sh"
if ($LASTEXITCODE -ne 0) {
    Write-Host "Install failed inside WSL (see output above). If curl is missing: wsl -e sudo apt-get install -y curl"
    exit 1
}
Write-Host "Start it with: wsl microcc /path/to/project"
