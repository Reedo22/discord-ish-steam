# discord-ish-steam — Windows installer.
# Run in PowerShell:  irm https://raw.githubusercontent.com/Reedo22/discord-ish-steam/master/install.ps1 | iex
# (or clone the repo and run .\install.ps1). Requires git + Millennium already installed.
#
# Installs the Millennium plugin + theme, then sets up the screen-share daemon
# (rp-webrtc.py): a localhost service that captures a monitor/window (ffmpeg gdigrab +
# NVENC/QSV), publishes via MediaMTX, and serves WebRTC/WHEP to the viewer's plugin.
$ErrorActionPreference = "Stop"

$repoUrl = "https://github.com/Reedo22/discord-ish-steam"
$repo = Join-Path $env:USERPROFILE "discord-ish-steam"

Write-Host "== discord-ish-steam Windows installer =="

# 1) clone or update the repo
if (Test-Path (Join-Path $repo ".git")) {
    Write-Host "Updating repo at $repo"; git -C $repo pull --ff-only
} else {
    Write-Host "Cloning into $repo"; git clone $repoUrl $repo
}

# 2) find Millennium by its config.json (the one with enabledPlugins)
Write-Host "Searching for Millennium config..."
$roots = @($env:LOCALAPPDATA, $env:APPDATA, $env:USERPROFILE,
    "C:\Program Files (x86)\Steam", "C:\Program Files\Steam") | Where-Object { $_ -and (Test-Path $_) }
$cfgFile = $null
foreach ($r in $roots) {
    $hit = Get-ChildItem -Path $r -Recurse -Depth 4 -Filter "config.json" -ErrorAction SilentlyContinue |
        Where-Object { try { (Get-Content $_.FullName -Raw) -match '"enabledPlugins"' } catch { $false } } |
        Select-Object -First 1
    if ($hit) { $cfgFile = $hit.FullName; break }
}
if (-not $cfgFile) { throw "Could not find Millennium config.json (with enabledPlugins). Is Millennium installed and run at least once?" }
$millennium = Split-Path $cfgFile -Parent
Write-Host "Found Millennium: $millennium"

$quickcss   = Join-Path $millennium "quickcss.css"
$pluginsDir = Join-Path $millennium "plugins"
New-Item -ItemType Directory -Force -Path $pluginsDir | Out-Null

# 3) write quickcss (theme baseline; the plugin also live-fetches the latest CSS on boot)
$css = Get-Content (Join-Path $repo "theme\friends.custom.css") -Raw
"/* Quick CSS file created by Millennium */`r`n/* discord-ish */`r`n$css" | Set-Content $quickcss -Encoding UTF8
Write-Host "Wrote theme to $quickcss"

# 4) install the plugin
$dest = Join-Path $pluginsDir "discordish-chat"
if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
Copy-Item (Join-Path $repo "plugin") $dest -Recurse -Force
Write-Host "Installed plugin to $dest"

# 5) enable the plugin in config.json
$cfg = Get-Content $cfgFile -Raw | ConvertFrom-Json
if (-not $cfg.plugins) { $cfg | Add-Member -NotePropertyName plugins -NotePropertyValue (@{ enabledPlugins = @() }) }
$enabled = @($cfg.plugins.enabledPlugins)
if ($enabled -notcontains "discordish-chat") {
    $cfg.plugins.enabledPlugins = $enabled + "discordish-chat"
    ($cfg | ConvertTo-Json -Depth 30) | Set-Content $cfgFile -Encoding UTF8
    Write-Host "Enabled discordish-chat in config.json"
} else { Write-Host "discordish-chat already enabled" }

# 6) screen-share daemon prerequisites
$py = $null
if (Get-Command python -ErrorAction SilentlyContinue) { $py = (Get-Command python) }
elseif (Get-Command py -ErrorAction SilentlyContinue) { $py = (Get-Command py) }
if (-not $py) { Write-Warning "Python not found - the screen-share daemon needs it. 'winget install Python.Python.3'." }
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Warning "ffmpeg not on PATH - screen-share capture needs it ('winget install Gyan.FFmpeg'). Theme/calls/voice still work."
}

# 7) fetch the Windows host binaries (MediaMTX + cloudflared) into bin\
& (Join-Path $repo "bin\fetch-windows.ps1")

# 8) register a logon task so the daemon is always up (mirrors the Linux systemd service)
if ($py) {
    $pyw = ($py.Source -replace "python\.exe$", "pythonw.exe")
    if (-not (Test-Path $pyw)) { $pyw = $py.Source }
    $daemon  = Join-Path $repo "rp-webrtc.py"
    $action  = New-ScheduledTaskAction -Execute $pyw -Argument "`"$daemon`""
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $set     = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName "discordish-rp-webrtc" -Action $action -Trigger $trigger -Settings $set -Force | Out-Null
    Write-Host "Registered logon task 'discordish-rp-webrtc' (auto-starts the screen-share daemon)."
    Start-Process $pyw -ArgumentList "`"$daemon`"" -WindowStyle Hidden
    Write-Host "Started the screen-share daemon."
}

Write-Host ""
Write-Host "DONE. Now: 1) fully restart Steam,  2) Friends settings -> enable 'Dock chats to the friends list'."
