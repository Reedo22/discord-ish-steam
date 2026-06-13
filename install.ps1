# discord-ish-steam — Windows installer (DRAFT: verify Millennium paths on a real
# Windows install before trusting). Requires git + Millennium already installed.
$ErrorActionPreference = "Stop"

$repo = Join-Path $env:USERPROFILE "discord-ish-steam"

# 1) clone or update the repo
if (Test-Path (Join-Path $repo ".git")) {
    git -C $repo pull --ff-only
} else {
    git clone https://github.com/Reedo22/discord-ish-steam $repo
}

# 2) locate Millennium (VERIFY — common candidates)
$candidates = @(
    (Join-Path $env:LOCALAPPDATA "Millennium"),
    (Join-Path $env:APPDATA "Millennium"),
    "C:\Program Files (x86)\Steam\plugins\..\millennium"
)
$millennium = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $millennium) { throw "Millennium dir not found. Edit `$candidates in this script." }
Write-Host "Using Millennium dir: $millennium"

$quickcss = Join-Path $millennium "quickcss.css"
$pluginsDir = Join-Path $millennium "plugins"
$configJson = Join-Path $millennium "config.json"

# 3) write quickcss from the theme
$css = Get-Content (Join-Path $repo "theme\friends.custom.css") -Raw
"/* Quick CSS file created by Millennium */`r`n/* discord-ish */`r`n$css" | Set-Content -NoNewline $quickcss

# 4) install the plugin
$dest = Join-Path $pluginsDir "discordish-chat"
if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
Copy-Item (Join-Path $repo "plugin") $dest -Recurse -Force

# 5) enable it in config.json
if (Test-Path $configJson) {
    $cfg = Get-Content $configJson -Raw | ConvertFrom-Json
    if (-not $cfg.plugins) { $cfg | Add-Member plugins (@{ enabledPlugins = @() }) }
    if ($cfg.plugins.enabledPlugins -notcontains "discordish-chat") {
        $cfg.plugins.enabledPlugins += "discordish-chat"
        $cfg | ConvertTo-Json -Depth 20 | Set-Content $configJson
    }
}

Write-Host "Installed. Restart Steam, and enable 'Dock chats to the friends list' in Friends settings."
