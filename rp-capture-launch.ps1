# Windows launcher for the Discord-ish Remote Play capture server.
# Steam runs this as a non-Steam shortcut:
#   powershell -File rp-capture-launch.ps1 <geom|primary|secondary> <res WxH|none> <inviteSteamID64>
# It points the capture server at the bundled RemotePlayWhatever and starts it
# (the server runs RPW to create the RPT session + invite, then ffplay-gdigrab
# shows the chosen monitor/app; the control server handles live switching).
param([string]$Geom = "primary", [string]$Res = "1920x1080", [string]$Invite = "none")
$ErrorActionPreference = "SilentlyContinue"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:RPW_PATH = Join-Path $here "bin\RemotePlayWhatever.exe"
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (Get-Command py -ErrorAction SilentlyContinue).Source }
if (-not $py) { exit 1 }    # Python required (install.ps1 warns if missing)
& $py (Join-Path $here "rp-capture.py") $Geom $Res $Invite
