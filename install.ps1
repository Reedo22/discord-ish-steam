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
    Write-Host "Updating repo at $repo"
    try { git -C $repo pull --ff-only 2>&1 | Out-Host; if ($LASTEXITCODE -ne 0) { throw "pull exit $LASTEXITCODE" } }
    catch { Write-Warning "git pull failed ($($_.Exception.Message)) - continuing with the existing clone." }
} else {
    Write-Host "Cloning into $repo"; git clone $repoUrl $repo
}

# 2) find Millennium's config.json. Millennium stores it at
#    <SteamPath>\millennium\config\config.json, where SteamPath comes from the
#    registry (HKCU\Software\Valve\Steam\SteamPath) - NOT a fixed Program Files path,
#    so this works for Steam installs on any drive. Falls back to a filesystem search.
Write-Host "Locating Millennium..."
$steamPath = $null
try { $steamPath = (Get-ItemProperty 'HKCU:\Software\Valve\Steam' -ErrorAction Stop).SteamPath } catch {}
if ($steamPath) { $steamPath = $steamPath -replace '/', '\' }
$cfgFile = $null
if ($steamPath) {
    $candidate = Join-Path $steamPath "millennium\config\config.json"
    if (Test-Path $candidate) { $cfgFile = $candidate }
}
if (-not $cfgFile) {
    Write-Host "  registry path missed; searching the filesystem..."
    $roots = @($steamPath, $env:LOCALAPPDATA, $env:APPDATA, $env:USERPROFILE,
        "C:\Program Files (x86)\Steam", "C:\Program Files\Steam", "D:\Steam", "D:\SteamLibrary") |
        Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique
    foreach ($r in $roots) {
        $hit = Get-ChildItem -Path $r -Recurse -Depth 5 -Filter "config.json" -ErrorAction SilentlyContinue |
            Where-Object { try { (Get-Content $_.FullName -Raw) -match '"enabledPlugins"' } catch { $false } } |
            Select-Object -First 1
        if ($hit) { $cfgFile = $hit.FullName; break }
    }
}
if (-not $cfgFile) { throw "Could not find Millennium's config.json. Install Millennium (https://steambrew.app), launch Steam and log in once, then re-run." }
# config.json lives in a "config" subfolder; plugins\ and quick.css live in the
# Millennium root (the parent). Walk up only when we're actually in that subfolder.
$configDir = Split-Path $cfgFile -Parent                  # e.g. <steam>\millennium\config
if ((Split-Path $configDir -Leaf) -eq "config") { $millRoot = Split-Path $configDir -Parent }
else { $millRoot = $configDir }                           # older flat layout
Write-Host "Found Millennium config: $cfgFile"

$quickcss   = Join-Path $configDir "quick.css"            # Millennium v3 reads quick.css
$pluginsDir = Join-Path $millRoot  "plugins"              # plugins live beside config/, not under it
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
try { & (Join-Path $repo "bin\fetch-windows.ps1") }
catch { Write-Warning "Couldn't fetch host binaries ($($_.Exception.Message)). Re-run bin\fetch-windows.ps1 later; theme/plugin still installed." }

# 8) register a logon task so the daemon is always up (mirrors the Linux systemd service)
if ($py) {
    $pyw = ($py.Source -replace "python\.exe$", "pythonw.exe")
    if (-not (Test-Path $pyw)) { $pyw = $py.Source }
    $daemon  = Join-Path $repo "rp-webrtc.py"
    $action  = New-ScheduledTaskAction -Execute $pyw -Argument "`"$daemon`""
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $set     = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
    try {
        Register-ScheduledTask -TaskName "discordish-rp-webrtc" -Action $action -Trigger $trigger -Settings $set -Force | Out-Null
        Write-Host "Registered logon task 'discordish-rp-webrtc' (auto-starts the screen-share daemon)."
    } catch {
        # Task registration can need elevation; fall back to a Startup-folder shortcut so
        # the daemon still auto-starts at logon without aborting the install.
        Write-Warning "Couldn't register the logon task ($($_.Exception.Message)) - using a Startup shortcut instead."
        try {
            $startup = [Environment]::GetFolderPath('Startup')
            $lnk = Join-Path $startup "discordish-rp-webrtc.lnk"
            $ws = New-Object -ComObject WScript.Shell
            $sc = $ws.CreateShortcut($lnk)
            $sc.TargetPath = $pyw; $sc.Arguments = "`"$daemon`""; $sc.WindowStyle = 7; $sc.Save()
            Write-Host "Created Startup shortcut $lnk"
        } catch { Write-Warning "Startup shortcut also failed ($($_.Exception.Message)); start rp-webrtc.py manually." }
    }
    Start-Process $pyw -ArgumentList "`"$daemon`"" -WindowStyle Hidden
    Write-Host "Started the screen-share daemon."
}

Write-Host ""
Write-Host "DONE. Now: 1) fully restart Steam,  2) Friends settings -> enable 'Dock chats to the friends list'."
