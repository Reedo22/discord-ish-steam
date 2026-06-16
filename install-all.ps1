# discord-ish-steam — Windows ALL-IN-ONE installer.
# From a base Steam install to the finished reskin in one command:
#   prerequisites (git, Python, ffmpeg) -> Millennium -> Steam first-run -> plugin + daemon.
# Run in PowerShell:
#   irm https://raw.githubusercontent.com/Reedo22/discord-ish-steam/master/install-all.ps1 | iex
$ErrorActionPreference = "Stop"
Write-Host "== discord-ish-steam — all-in-one Windows installer =="

# 1) prerequisites via winget
function Need($cmd, $id) {
  if (Get-Command $cmd -ErrorAction SilentlyContinue) { Write-Host "  $cmd present"; return }
  if (Get-Command winget -ErrorAction SilentlyContinue) {
    Write-Host "  installing $id ..."
    winget install -e --id $id --accept-source-agreements --accept-package-agreements --silent
  } else { Write-Warning "  winget not available - install $cmd manually." }
}
Need git    Git.Git
Need python Python.Python.3
Need ffmpeg Gyan.FFmpeg
# make the freshly-installed tools visible in this session
$env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")

# 2) Steam present?
$steam = @("C:\Program Files (x86)\Steam\steam.exe","C:\Program Files\Steam\steam.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $steam) { Write-Warning "Steam not found - install it from https://store.steampowered.com first, then re-run."; }

# 3) Millennium (official signed installer)
Write-Host "Installing Millennium ..."
try { iwr -useb "https://steambrew.app/install.ps1" | iex } catch { Write-Warning "Millennium install reported: $_" }

# 4) Millennium writes its config.json only after Steam runs once with it. Launch Steam and wait.
function Find-MillenniumCfg {
  $roots = @($env:USERPROFILE,$env:LOCALAPPDATA,$env:APPDATA) | Where-Object { $_ -and (Test-Path $_) }
  foreach ($r in $roots) {
    $hit = Get-ChildItem -Path $r -Recurse -Depth 4 -Filter config.json -ErrorAction SilentlyContinue |
      Where-Object { try { (Get-Content $_.FullName -Raw) -match '"enabledPlugins"' } catch { $false } } | Select-Object -First 1
    if ($hit) { return $hit.FullName }
  }
  return $null
}
$cfg = Find-MillenniumCfg
if (-not $cfg) {
  if ($steam) { Write-Host "Launching Steam so Millennium initializes (log in if prompted) ..."; Start-Process $steam }
  for ($i = 0; $i -lt 120 -and -not $cfg; $i++) { Start-Sleep 5; $cfg = Find-MillenniumCfg }
}
if (-not $cfg) { throw "Millennium's config.json never appeared. Finish logging into Steam, then run install.ps1 to complete." }
Write-Host "Millennium ready."

# 5) install this plugin + theme + daemon (reuses the standard installer)
Write-Host "Installing discord-ish-steam ..."
iwr -useb "https://raw.githubusercontent.com/Reedo22/discord-ish-steam/master/install.ps1" | iex

Write-Host ""
Write-Host "ALL DONE. Fully restart Steam, then enable Settings -> Friends & Chat -> 'Dock chats to the friends list'."
