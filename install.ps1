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

# 6) screen-share daemon prerequisites (Python + ffmpeg) — auto-install via winget if missing.
# GOTCHA: the Microsoft Store ships a fake "python.exe" stub (under ...\WindowsApps\) that only
# nags to install from the Store. Get-Command python MATCHES it, so a naive check thinks Python
# is present, skips the install, and points the daemon at a stub that never runs (= empty share
# picker). We resolve REAL Python via the `py` launcher (never shadowed by the stub) and treat
# the WindowsApps stub as absent.
function Update-SessionPath {
    $m = [Environment]::GetEnvironmentVariable('Path','Machine')
    $u = [Environment]::GetEnvironmentVariable('Path','User')
    $env:Path = (@($m, $u) | Where-Object { $_ }) -join ';'
}
function Get-RealPyExe {
    # The Store python.exe stub prints a nag and exits non-zero; REAL Python (even a Store-
    # installed one living under WindowsApps\) returns its sys.executable. So we EXECUTE each
    # candidate and trust whatever behaves like Python — never path-match, never assume `py`
    # exists (a missing `py` must not crash the installer).
    $tries = @(
        @{ exe = 'py';      pre = @('-3') },
        @{ exe = 'python';  pre = @() },
        @{ exe = 'python3'; pre = @() }
    )
    foreach ($t in $tries) {
        try {
            $a = @($t.pre + @('-c', 'import sys;print(sys.executable)'))
            $out = (& $t.exe @a 2>$null)
            if ($LASTEXITCODE -eq 0 -and $out) {
                $p = ($out | Select-Object -Last 1).ToString().Trim()
                if ($p -and (Test-Path $p)) { return $p }
            }
        } catch { }
    }
    return $null
}
$winget = Get-Command winget -ErrorAction SilentlyContinue
if (-not (Get-RealPyExe)) {
    if ($winget) { Write-Host "Installing Python (winget)..."; winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements | Out-Null; Update-SessionPath }
    else { Write-Warning "Real Python missing (the Store stub doesn't count) and winget unavailable - install Python 3 from python.org, then re-run." }
}
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    if ($winget) { Write-Host "Installing ffmpeg (winget)..."; winget install -e --id Gyan.FFmpeg --silent --accept-package-agreements --accept-source-agreements | Out-Null; Update-SessionPath }
    else { Write-Warning "ffmpeg missing and winget unavailable - install ffmpeg manually, then re-run." }
}
$pyExe = Get-RealPyExe
if (-not $pyExe) { Write-Warning "Real Python still not found (Store stub?) - reopen the terminal, or disable the python App Execution Aliases (Settings > Apps > Advanced app settings), then re-run." }
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) { Write-Warning "ffmpeg still not on PATH - reopen the terminal / reboot so the daemon can find it." }

# 7) fetch the Windows host binaries (MediaMTX + cloudflared) into bin\
try { & (Join-Path $repo "bin\fetch-windows.ps1") }
catch { Write-Warning "Couldn't fetch host binaries ($($_.Exception.Message)). Re-run bin\fetch-windows.ps1 later; theme/plugin still installed." }

# 8) register a logon task so the daemon is always up (mirrors the Linux systemd service)
if ($pyExe) {
    $pyw = ($pyExe -replace "python\.exe$", "pythonw.exe")
    if (-not (Test-Path $pyw)) { $pyw = $pyExe }
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
