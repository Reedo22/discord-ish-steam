# discord-ish-steam — Windows installer (self-discovering).
# Run in PowerShell:  irm https://raw.githubusercontent.com/Reedo22/discord-ish-steam/master/install.ps1 | iex
# (or clone the repo and run this script). Requires git + Millennium already installed.
$ErrorActionPreference = "Stop"

$repoUrl = "https://github.com/Reedo22/discord-ish-steam"
$repo = Join-Path $env:USERPROFILE "discord-ish-steam"

Write-Host "== discord-ish-steam Windows installer =="

# 1) clone or update the repo
if (Test-Path (Join-Path $repo ".git")) {
    Write-Host "Updating repo at $repo"
    git -C $repo pull --ff-only
} else {
    Write-Host "Cloning into $repo"
    git clone $repoUrl $repo
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

# 3) write quickcss (theme)
$css = Get-Content (Join-Path $repo "theme\friends.custom.css") -Raw
"/* Quick CSS file created by Millennium */`r`n/* discord-ish */`r`n$css" | Set-Content $quickcss -Encoding UTF8
Write-Host "Wrote theme to $quickcss"

# 4) copy plugin + patch the Windows capture path to this clone
$dest = Join-Path $pluginsDir "discordish-chat"
if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
Copy-Item (Join-Path $repo "plugin") $dest -Recurse -Force
$idx = Join-Path $dest ".millennium\Dist\index.js"
$launcher = (Join-Path $repo "rp-capture-launch.ps1").Replace('\','\\')
(Get-Content $idx -Raw).Replace('C:\\Users\\reedo\\discord-ish-steam\\rp-capture-launch.ps1', $launcher) |
    Set-Content $idx -Encoding UTF8
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

# 6) (RemotePlayWhatever removed) The Remote Play session + invite are now created
#    natively by the plugin via the Spacewar/appid-480 launch hijack, same as Linux —
#    no external binary. Spacewar (free, appid 480) must be installed in your library.
$spacewar = $false
foreach ($r in $roots) {
    if (Test-Path (Join-Path $r "steamapps\common\Spacewar")) { $spacewar = $true; break }
}
if (-not $spacewar) {
    Write-Warning "Spacewar (appid 480) not detected - install it free from your Steam library (search 'Spacewar') so Remote Play screen-share works."
}

# 7) checks
if (-not (Get-Command python -ErrorAction SilentlyContinue) -and -not (Get-Command py -ErrorAction SilentlyContinue)) {
    Write-Warning "Python not found - the screen-share capture server needs it. Install from python.org or 'winget install Python.Python.3'."
}
if (-not (Get-Command ffplay -ErrorAction SilentlyContinue)) {
    Write-Warning "ffplay/ffmpeg not on PATH - screen-share capture needs it ('winget install Gyan.FFmpeg'). Theme/calls/voice still work."
}
Write-Host ""
Write-Host "DONE. Now: 1) fully restart Steam,  2) Friends settings -> enable 'Dock chats to the friends list'."
Write-Host "If something didn't load, paste this output back and we'll fix the path."
