# Windows launcher for the Discord-ish Remote Play capture server.
# Invoked by the hijacked Spacewar (appid 480) launch options:
#   powershell -File rp-capture-launch.ps1 <geom|primary|secondary> <res WxH|none> %command%
# Steam expands %command% to the real Spacewar exe path + args; those land in
# $Rest and are ignored (we just need Steam to think 480 is running so the RPT
# group is created). The invite itself is sent natively from the plugin (JS) —
# RemotePlayWhatever is no longer used. The capture server then shows the chosen
# monitor/app (ffplay gdigrab) and handles live switching over localhost:48591.
param(
  [string]$Geom = "primary",
  [string]$Res = "1920x1080",
  [Parameter(ValueFromRemainingArguments = $true)] $Rest
)
$ErrorActionPreference = "SilentlyContinue"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (Get-Command py -ErrorAction SilentlyContinue).Source }
if (-not $py) { exit 1 }    # Python required (install.ps1 warns if missing)
& $py (Join-Path $here "rp-capture.py") $Geom $Res
