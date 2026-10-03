# micro-cc's bash_ tool needs a POSIX shell, so on Windows this installs inside WSL, setting WSL up first if needed.
# MICROCC_WSL_INSTALL=1 sets WSL up without asking, =0 never does.
$Raw = "https://raw.githubusercontent.com/GSequist/micro-cc/main/install.sh"

# First usable non-Docker distro, or $null. wsl.exe prints UTF-16, hence the encoding swap.
function Get-Distro {
    if (-not (Get-Command wsl -ErrorAction SilentlyContinue)) { return $null }
    $enc = [Console]::OutputEncoding
    [Console]::OutputEncoding = [Text.Encoding]::Unicode
    try { $names = wsl -l -q 2>$null; $ok = ($LASTEXITCODE -eq 0) } finally { [Console]::OutputEncoding = $enc }
    if (-not $ok) { return $null }
    foreach ($n in $names) {
        $n = "$n".Trim()
        if (-not $n -or $n -match '^docker-desktop') { continue }
        wsl -d $n -e true 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { return $n }
    }
    return $null
}

function Install-Wsl {
    if ($env:MICROCC_WSL_INSTALL -eq "0") { return $false }
    if ($env:MICROCC_WSL_INSTALL -ne "1") {
        $a = Read-Host "micro-cc runs inside WSL (Linux on Windows), which isn't set up here. Install it now? Needs admin rights and may need a reboot [y/N]"
        if ($a -notmatch '^[yY]') { return $false }
    }
    $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if ($admin) { wsl --install -d Ubuntu --no-launch | Out-Host; $code = $LASTEXITCODE }
    else { $code = (Start-Process powershell -Verb RunAs -Wait -PassThru -ArgumentList "-NoProfile", "-Command", "wsl --install -d Ubuntu --no-launch").ExitCode }
    Write-Host "wsl --install exited with code $code"
    return $true
}

# A fresh Ubuntu would stop to ask for a username on first launch; create one and make it the default.
function Initialize-Ubuntu {
    wsl -d Ubuntu -u root -e true 2>$null | Out-Host
    if ($LASTEXITCODE -ne 0) { return $false }
    $u = ($env:USERNAME.ToLower() -replace '[^a-z0-9_]', '')
    if (-not $u) { $u = "user" } elseif ($u -match '^\d') { $u = "u$u" }
    wsl -d Ubuntu -u root -e sh -c "id -u $u >/dev/null 2>&1 || useradd -m -s /bin/bash $u; printf '[user]\ndefault=$u\n' > /etc/wsl.conf" | Out-Host
    wsl --terminate Ubuntu | Out-Host
    return $true
}

$distro = Get-Distro
if (-not $distro) {
    if (-not (Install-Wsl)) {
        Write-Host "WSL is required. Run 'wsl --install' in an admin PowerShell, reboot, then run this again."
        exit 1
    }
    if (-not (Initialize-Ubuntu)) {
        Write-Host "WSL is installed but needs a reboot to finish. Restart Windows, then run this command again."
        exit 1
    }
    $distro = "Ubuntu"
}

wsl -d $distro -u root -e sh -c "command -v curl >/dev/null || (apt-get update -qq && apt-get install -y -qq curl ca-certificates)"
wsl -d $distro -e sh -c "curl -fsSL $Raw | sh"
if ($LASTEXITCODE -ne 0) {
    Write-Host "Install failed inside WSL (see output above)."
    exit 1
}
Write-Host "Start it with: wsl -d $distro microcc /path/to/project"
