# discord-ish-steam — Windows installer.
# Run in PowerShell:  irm https://raw.githubusercontent.com/Reedo22/discord-ish-steam/master/install.ps1 | iex
# (or clone the repo and run .\install.ps1). Requires git + Millennium already installed.
#
# Installs the Millennium plugin + theme, then sets up the screen-share daemon
# (rp-webrtc.py): a localhost service that captures a monitor/window (ffmpeg gdigrab +
# NVENC/QSV), publishes via MediaMTX, and serves WebRTC/WHEP to the viewer's plugin.
$ErrorActionPreference = "Stop"
# Old PowerShell/.NET defaults to TLS 1.0 -> GitHub refuses -> clones/downloads fail.
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}

# DS_REPO_URL overrides the source (a fork, or a git bundle when testing unpushed work).
$repoUrl = if ($env:DS_REPO_URL) { $env:DS_REPO_URL } else { "https://github.com/Reedo22/discord-ish-steam" }
$repo = Join-Path $env:USERPROFILE "discord-ish-steam"
# What to check out. Normal installs track master; CI sets DS_REPO_REF to the commit under
# test so the installer installs THAT code rather than whatever master currently is.
$repoRef = if ($env:DS_REPO_REF) { $env:DS_REPO_REF } else { "origin/master" }

Write-Host "== discord-ish-steam Windows installer =="

function Update-SessionPath {
    $m = [Environment]::GetEnvironmentVariable('Path','Machine')
    $u = [Environment]::GetEnvironmentVariable('Path','User')
    $env:Path = (@($m, $u) | Where-Object { $_ }) -join ';'
}
# Run a native program, streaming its output to the log; returns its exit code. Needed
# because under Windows PowerShell 5.1 with $ErrorActionPreference = "Stop", ANY line a
# native program writes to stderr becomes a terminating error — and git writes its normal
# progress there ("Cloning into ...", "From https://..."). That killed every fresh clone
# (found in the Windows 11 test VM), and made re-runs report "git update failed" and keep
# the OLD copy whenever there was something new to fetch.
function Invoke-Native {
    $exe, $rest = $args
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { & $exe @rest 2>&1 | ForEach-Object { "$_" } | Out-Host; return $LASTEXITCODE }
    finally { $ErrorActionPreference = $old }
}
# winget, pinned to the "winget" source. Without --source it ALSO queries the Microsoft
# Store source, which on a fresh PC isn't initialised yet: the search fails and the install
# silently does nothing (found in the Windows 11 test VM: git never got installed, so the
# clone died). Output is shown, not swallowed, so a real failure is visible in the log.
function Install-WingetPackage($id, $label) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Warning "$label is missing and winget isn't available - install $label manually, then re-run."
        return
    }
    Write-Host "Installing $label (winget)..."
    $rc = Invoke-Native winget install -e --id $id --source winget --silent --accept-package-agreements --accept-source-agreements
    if ($rc -ne 0) { Write-Warning "winget couldn't install $label (exit $rc) - see above." }
    Update-SessionPath
}

# 0) ensure git (needed for the clone in step 1). The one-click setup.exe runs us
#    elevated, so winget can actually install here; if winget is absent we warn and
#    the clone below will surface the missing-git error.
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Install-WingetPackage "Git.Git" "git" }
foreach ($g in @("$env:ProgramFiles\Git\cmd", "${env:ProgramFiles(x86)}\Git\cmd", "$env:LOCALAPPDATA\Programs\Git\cmd")) {
    if (-not (Get-Command git -ErrorAction SilentlyContinue) -and (Test-Path (Join-Path $g "git.exe"))) { $env:Path = "$g;$env:Path" }
}

# 1) clone or update the repo. Robust to three states: a fresh machine, an
#    existing git checkout, OR a stale NON-git copy from an older/manual install
#    (that last case made `git clone` hard-fail with "destination already exists").
function Sync-Repo {
    if ((Test-Path (Join-Path $repo ".git")) -and (Get-Command git -ErrorAction SilentlyContinue)) {
        # Existing checkout: hard-reset to latest master (self-healing updater;
        # discards any local edits, which is what we want on a friend's machine).
        Write-Host "Updating repo at $repo"
        $rc = Invoke-Native git -C $repo fetch origin
        if ($rc -eq 0) { $rc = Invoke-Native git -C $repo reset --hard $repoRef }
        if ($rc -ne 0) { Write-Warning "git update failed (exit $rc) - continuing with the existing copy." }
        return
    }
    if (Test-Path $repo) {
        # Folder exists but isn't a git checkout. Adopt it IN PLACE (init + fetch +
        # reset) so already-fetched bin\ binaries survive, instead of failing.
        Write-Host "Found a non-git copy at $repo - converting it to a git checkout"
        if (Get-Command git -ErrorAction SilentlyContinue) {
            $null = Invoke-Native git -C $repo init
            $null = Invoke-Native git -C $repo remote remove origin
            $rc = Invoke-Native git -C $repo remote add origin $repoUrl
            if ($rc -eq 0) { $rc = Invoke-Native git -C $repo fetch origin }
            if ($rc -eq 0) { $rc = Invoke-Native git -C $repo reset --hard $repoRef }
            if ($rc -eq 0) { return }
            Write-Warning "Couldn't adopt the existing folder (git exit $rc) - re-downloading fresh."
        }
        $bak = "$repo.bak"
        if (Test-Path $bak) { Remove-Item $bak -Recurse -Force }
        try { Rename-Item $repo $bak -Force } catch { Remove-Item $repo -Recurse -Force }
    }
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        # No git at all (winget missing/broken): fetch the repo as a zip instead. Updating
        # later = re-running this installer, which downloads it again.
        Write-Warning "git unavailable - downloading the repo as a zip instead."
        $zip = Join-Path $env:TEMP "discord-ish-steam.zip"
        Invoke-WebRequest -UseBasicParsing "$repoUrl/archive/refs/heads/master.zip" -OutFile $zip
        $x = Join-Path $env:TEMP "discord-ish-steam-zip"
        if (Test-Path $x) { Remove-Item $x -Recurse -Force }
        Expand-Archive $zip -DestinationPath $x -Force
        $src = Get-ChildItem $x -Directory | Select-Object -First 1
        New-Item -ItemType Directory -Force $repo | Out-Null
        Copy-Item (Join-Path $src.FullName "*") $repo -Recurse -Force
        return
    }
    Write-Host "Cloning into $repo"
    $rc = Invoke-Native git clone $repoUrl $repo
    if ($rc -eq 0 -and $env:DS_REPO_REF) { $rc = Invoke-Native git -C $repo reset --hard $repoRef }
    if ($rc -ne 0 -or -not (Test-Path (Join-Path $repo ".git"))) { throw "git clone failed - check internet and that git installed correctly, then re-run." }
}
Sync-Repo

# 2) find Millennium's config.json. Millennium stores it at
#    <SteamPath>\millennium\config\config.json, where SteamPath comes from the
#    registry (HKCU\Software\Valve\Steam\SteamPath) - NOT a fixed Program Files path,
#    so this works for Steam installs on any drive. Falls back to a filesystem search.
Write-Host "Locating Millennium..."
$steamPath = $null
try { $steamPath = (Get-ItemProperty 'HKCU:\Software\Valve\Steam' -ErrorAction Stop).SteamPath } catch {}
if ($steamPath) { $steamPath = $steamPath -replace '/', '\' }
$steamExe = if ($steamPath) { Join-Path $steamPath "steam.exe" } else { $null }

function Find-MillenniumConfig {
    if ($steamPath) {
        $candidate = Join-Path $steamPath "millennium\config\config.json"
        if (Test-Path $candidate) { return $candidate }
    }
    Write-Host "  registry path missed; searching the filesystem..."
    $roots = @($steamPath, $env:LOCALAPPDATA, $env:APPDATA, $env:USERPROFILE,
        "C:\Program Files (x86)\Steam", "C:\Program Files\Steam", "D:\Steam", "D:\SteamLibrary") |
        Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique
    foreach ($r in $roots) {
        $hit = Get-ChildItem -Path $r -Recurse -Depth 5 -Filter "config.json" -ErrorAction SilentlyContinue |
            Where-Object { try { (Get-Content $_.FullName -Raw) -match '"enabledPlugins"' } catch { $false } } |
            Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    return $null
}

# Steam has to be started as the NORMAL user: this script runs elevated (UAC), and a Steam
# launched from here would inherit admin rights. explorer.exe hands the launch to the shell,
# which runs it un-elevated.
function Stop-Steam {
    if (-not (Get-Process steam -ErrorAction SilentlyContinue)) { return }
    Write-Host "  closing Steam..."
    if ($steamExe -and (Test-Path $steamExe)) { & $steamExe -shutdown 2>$null }
    foreach ($i in 1..30) { if (-not (Get-Process steam -ErrorAction SilentlyContinue)) { return }; Start-Sleep 1 }
    Get-Process steam, steamwebhelper -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep 2
}
function Start-SteamAsUser { if ($steamExe -and (Test-Path $steamExe)) { Start-Process explorer.exe -ArgumentList "`"$steamExe`"" } }

# Millennium missing -> install it ourselves (the friend shouldn't need a second installer).
# Its Windows release is a zip that unpacks into the Steam folder (wsock32.dll + millennium\);
# we verify it against the release's published SHA-256 before touching Steam.
function Install-Millennium {
    Write-Host "Millennium not found - installing it into $steamPath"
    $rel = Invoke-RestMethod "https://api.github.com/repos/SteamClientHomebrew/Millennium/releases/latest" -Headers @{ "User-Agent" = "discord-ish-steam" }
    $zipA = $rel.assets | Where-Object { $_.name -like "millennium-*-windows-x86_64.zip" } | Select-Object -First 1
    $shaA = $rel.assets | Where-Object { $_.name -like "millennium-*-windows-x86_64.sha256" } | Select-Object -First 1
    if (-not $zipA -or -not $shaA) { throw "Millennium release $($rel.tag_name) has no Windows zip" }
    $zip = Join-Path $env:TEMP $zipA.name
    Invoke-WebRequest -UseBasicParsing $zipA.browser_download_url -OutFile $zip
    # (download to a file: PS 5.1 hands back an octet-stream body as a byte[], not text)
    $shaFile = "$zip.sha256"
    Invoke-WebRequest -UseBasicParsing $shaA.browser_download_url -OutFile $shaFile
    $want = ((Get-Content $shaFile -Raw).Trim() -split '\s+')[0].ToLower()
    $got = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
    if ($want -ne $got) { throw "Millennium download checksum mismatch (expected $want, got $got)" }
    Stop-Steam
    Expand-Archive -Path $zip -DestinationPath $steamPath -Force
    Write-Host "  installed Millennium $($rel.tag_name); starting Steam once so it creates its config..."
    Start-SteamAsUser
    foreach ($i in 1..120) {
        if (Test-Path (Join-Path $steamPath "millennium\config\config.json")) { break }
        Start-Sleep 1
    }
}

$cfgFile = Find-MillenniumConfig
if (-not $cfgFile -and $steamExe -and (Test-Path $steamExe)) {
    try { Install-Millennium } catch { Write-Warning "Automatic Millennium install failed: $($_.Exception.Message)" }
    $cfgFile = Find-MillenniumConfig
}
if (-not $cfgFile) {
    if (-not $steamExe -or -not (Test-Path $steamExe)) { throw "Steam isn't installed (or never ran). Install Steam, log in once, then re-run." }
    throw "Could not find Millennium's config.json. Install Millennium (https://steambrew.app), launch Steam and log in once, then re-run."
}
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
    # PATH is often STALE in this session - notably right after the winget install above, where
    # Update-SessionPath doesn't always take. That used to leave $pyExe null, which skipped the
    # daemon autostart block entirely and left the user starting rp-webrtc.py by hand forever.
    # So also sweep the registry and the usual install dirs, and validate each the same way
    # everything else here is validated: by RUNNING it.
    foreach ($hive in @('HKLM:\SOFTWARE\Python\PythonCore',
                        'HKCU:\SOFTWARE\Python\PythonCore',
                        'HKLM:\SOFTWARE\WOW6432Node\Python\PythonCore')) {
        try {
            foreach ($k in (Get-ChildItem $hive -ErrorAction Stop)) {
                $ip = (Get-ItemProperty -Path (Join-Path $k.PSPath 'InstallPath') -ErrorAction SilentlyContinue).'(default)'
                if ($ip) { $tries += @{ exe = (Join-Path $ip 'python.exe'); pre = @() } }
            }
        } catch { }
    }
    foreach ($d in @("$env:LOCALAPPDATA\Programs\Python", "$env:ProgramFiles", "${env:ProgramFiles(x86)}")) {
        try {
            foreach ($sub in (Get-ChildItem $d -Directory -Filter 'Python3*' -ErrorAction Stop)) {
                $tries += @{ exe = (Join-Path $sub.FullName 'python.exe'); pre = @() }
            }
        } catch { }
    }
    foreach ($t in $tries) {
        # a bare name must go through PATH resolution; a full path that isn't there is skipped
        if ($t.exe -match '[\\/]' -and -not (Test-Path $t.exe)) { continue }
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
    Install-WingetPackage "Python.Python.3.12" "Python"
}
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Install-WingetPackage "Gyan.FFmpeg" "ffmpeg"
}
$pyExe = Get-RealPyExe
if (-not $pyExe) { Write-Warning "Real Python still not found (Store stub?) - reopen the terminal, or disable the python App Execution Aliases (Settings > Apps > Advanced app settings), then re-run." }
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) { Write-Warning "ffmpeg still not on PATH - reopen the terminal / reboot so the daemon can find it." }

# 6b) desktop-audio capture needs pyaudiowpatch (WASAPI loopback, no driver). pip is
# idempotent, so this runs every install/update and self-heals a missing/old package.
if ($pyExe) {
    Write-Host "Ensuring pyaudiowpatch (desktop-audio capture)..."
    try {
        $rc = Invoke-Native $pyExe -m pip install --upgrade --disable-pip-version-check pyaudiowpatch
        if ($rc -ne 0) { throw "pip exit $rc" }
    } catch { Write-Warning "pyaudiowpatch install failed ($($_.Exception.Message)) - screen share works but WITHOUT audio until 'python -m pip install pyaudiowpatch' succeeds." }
}

# 7) fetch the Windows host binaries (MediaMTX + cloudflared) into bin\ - runs every time;
# fetch-windows.ps1 is idempotent (keeps present binaries, (re)fetches missing ones).
try { & (Join-Path $repo "bin\fetch-windows.ps1") | Out-Host }
catch { Write-Warning "Couldn't fetch host binaries ($($_.Exception.Message)). Re-run bin\fetch-windows.ps1 later; theme/plugin still installed." }
# Verify the binaries actually landed (the earlier failure mode was a silent partial fetch).
$mtxOk = Test-Path (Join-Path $repo "bin\mediamtx.exe")
$cfOk  = Test-Path (Join-Path $repo "bin\cloudflared.exe")
if (-not $mtxOk) { Write-Warning "mediamtx.exe missing - self-preview + LAN sharing won't work. Re-run: .\bin\fetch-windows.ps1" }
if (-not $cfOk)  { Write-Host "  (cloudflared.exe absent - off-LAN sharing disabled; LAN sharing still works.)" }

# 7b) firewall: allow the share's inbound ports up front. Otherwise the first share pops a
# Windows Defender "allow access?" dialog for python/mediamtx, and Cancel (or a network set
# to Public, the Windows 11 default) silently blocks LAN viewers and direct WebRTC.
# UDP 8189 = WebRTC media (MediaMTX); TCP 48890 = the daemon's key-checked media port.
try {
    Get-NetFirewallRule -DisplayName "discord-ish screen share*" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -DisplayName "discord-ish screen share (WebRTC media)" -Direction Inbound -Action Allow `
        -Protocol UDP -LocalPort 8189 -Program (Join-Path $repo "bin\mediamtx.exe") -Profile Any | Out-Null
    New-NetFirewallRule -DisplayName "discord-ish screen share (media port)" -Direction Inbound -Action Allow `
        -Protocol TCP -LocalPort 48890 -Profile Any | Out-Null
    Write-Host "Added firewall rules for screen share (UDP 8189, TCP 48890)."
} catch { Write-Warning "Couldn't add firewall rules ($($_.Exception.Message)) - Windows may ask to allow access on the first share." }

# 8) register a logon task so the daemon is always up (mirrors the Linux systemd service).
# This is now BELT-AND-BRACES: the plugin's Lua backend (plugin/backend/main.lua) also starts
# the daemon on every Steam launch, so if this fails the user is no longer stuck running
# rp-webrtc.py by hand. Deliberately NOT gated on $pyExe any more - fall back to whatever
# interpreter we can name, since the py launcher resolves a real one at task-run time.
$pyw = $null
$pywPre = ""
if ($pyExe) {
    $pyw = ($pyExe -replace "python\.exe$", "pythonw.exe")
    if (-not (Test-Path $pyw)) { $pyw = $pyExe }
} else {
    foreach ($n in @('pythonw', 'python', 'py')) {
        $c = Get-Command $n -ErrorAction SilentlyContinue
        if ($c) { $pyw = $c.Source; if ($n -eq 'py') { $pywPre = "-3 " }; break }
    }
    if ($pyw) { Write-Warning "Using '$pyw' for the logon task (couldn't resolve a real python.exe this session)." }
}
if ($pyw) {
    $daemon  = Join-Path $repo "rp-webrtc.py"
    $action  = New-ScheduledTaskAction -Execute $pyw -Argument "$pywPre`"$daemon`""
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $set     = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
    try {
        Register-ScheduledTask -TaskName "discordish-rp-webrtc" -Action $action -Trigger $trigger -Settings $set -Force | Out-Null
        # Register-ScheduledTask can report success and still leave nothing behind, so confirm.
        if (Get-ScheduledTask -TaskName "discordish-rp-webrtc" -ErrorAction SilentlyContinue) {
            Write-Host "Registered logon task 'discordish-rp-webrtc' (auto-starts the screen-share daemon)."
        } else {
            throw "task not present after registration"
        }
    } catch {
        # Task registration can need elevation; fall back to a Startup-folder shortcut so
        # the daemon still auto-starts at logon without aborting the install.
        Write-Warning "Couldn't register the logon task ($($_.Exception.Message)) - using a Startup shortcut instead."
        try {
            $startup = [Environment]::GetFolderPath('Startup')
            $lnk = Join-Path $startup "discordish-rp-webrtc.lnk"
            $ws = New-Object -ComObject WScript.Shell
            $sc = $ws.CreateShortcut($lnk)
            $sc.TargetPath = $pyw; $sc.Arguments = "$pywPre`"$daemon`""; $sc.WindowStyle = 7; $sc.Save()
            Write-Host "Created Startup shortcut $lnk"
        } catch {
            Write-Warning "Startup shortcut also failed ($($_.Exception.Message)) - not fatal: the plugin's Lua backend starts the daemon each time Steam launches."
        }
    }
    Start-Process $pyw -ArgumentList "$pywPre`"$daemon`"" -WindowStyle Hidden
    Write-Host "Started the screen-share daemon."
} else {
    Write-Warning "No Python interpreter found, so no logon task was registered. Install Python 3 and re-run; the plugin's Lua backend will then start the daemon on each Steam launch."
}

# Dependency summary — re-running the installer is the "updater"; this shows what's present.
function Test-Dep($name, $present) {
    if ($present) { Write-Host ("  [ok]   {0}" -f $name) }
    else          { Write-Warning ("[miss] {0}" -f $name) }
}
$pyaudioOk = $false
if ($pyExe) { try { & $pyExe -c "import pyaudiowpatch" 2>$null; $pyaudioOk = ($LASTEXITCODE -eq 0) } catch {} }
Write-Host ""
Write-Host "Dependency check:"
Test-Dep "Python"           ([bool]$pyExe)
Test-Dep "ffmpeg"           ([bool](Get-Command ffmpeg -ErrorAction SilentlyContinue))
Test-Dep "pyaudiowpatch (audio)" $pyaudioOk
Test-Dep "mediamtx.exe"     (Test-Path (Join-Path $repo "bin\mediamtx.exe"))
Test-Dep "cloudflared.exe (off-LAN)" (Test-Path (Join-Path $repo "bin\cloudflared.exe"))

# Restart Steam so the plugin loads right away (only if it's running; never start it on a
# machine where the user had it closed). DS_NO_STEAM_RESTART=1 skips this.
if (-not $env:DS_NO_STEAM_RESTART -and (Get-Process steam -ErrorAction SilentlyContinue)) {
    Write-Host "Restarting Steam to load the plugin..."
    Stop-Steam
    Start-SteamAsUser
}

Write-Host ""
Write-Host "DONE. In Steam: Friends settings -> enable 'Dock chats to the friends list'."
Write-Host "Re-run this installer any time to update + auto-install anything missing."
