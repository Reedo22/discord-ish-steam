# Fetch the Windows host binaries the screen-share daemon needs into this bin\ folder:
#   mediamtx.exe   - the RTSP->WebRTC server
#   cloudflared.exe - optional, only for sharing to friends OFF your LAN (https tunnel)
# Run:  .\bin\fetch-windows.ps1     (install.ps1 calls this automatically)
$ErrorActionPreference = "Stop"
$bin = $PSScriptRoot
Write-Host "Fetching Windows host binaries into $bin"

# --- MediaMTX (resolve latest windows_amd64 zip from the GitHub API) ---
$mtxExe = Join-Path $bin "mediamtx.exe"
if (Test-Path $mtxExe) {
    Write-Host "  mediamtx.exe already present - skipping"
} else {
    $rel = Invoke-RestMethod "https://api.github.com/repos/bluenviron/mediamtx/releases/latest" -Headers @{ "User-Agent" = "discord-ish-steam" }
    $asset = $rel.assets | Where-Object { $_.name -match "windows_amd64\.zip$" } | Select-Object -First 1
    if (-not $asset) { throw "Could not find a MediaMTX windows_amd64 asset in the latest release." }
    $zip = Join-Path $env:TEMP $asset.name
    Write-Host "  downloading $($asset.name)"
    Invoke-WebRequest $asset.browser_download_url -OutFile $zip
    Expand-Archive $zip -DestinationPath $bin -Force
    Remove-Item $zip -Force
    if (-not (Test-Path $mtxExe)) { throw "MediaMTX zip extracted but mediamtx.exe not found." }
    Write-Host "  installed mediamtx.exe"
}

# --- cloudflared (stable direct download; optional - off-LAN sharing only) ---
$cfExe = Join-Path $bin "cloudflared.exe"
if (Test-Path $cfExe) {
    Write-Host "  cloudflared.exe already present - skipping"
} else {
    try {
        Invoke-WebRequest "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" -OutFile $cfExe
        Write-Host "  installed cloudflared.exe"
    } catch {
        Write-Warning "  cloudflared.exe download failed - off-LAN sharing won't work, but LAN sharing will. ($_)"
    }
}

Write-Host "Done."
