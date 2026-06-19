# Fetch the Windows host binaries the screen-share daemon needs into this bin\ folder:
#   mediamtx.exe    - the RTSP->WebRTC server (self-preview + LAN viewers)
#   cloudflared.exe - off-LAN sharing only (https tunnel to friends not on your LAN)
# Run:  .\bin\fetch-windows.ps1     (install.ps1 calls this automatically every run)
# Idempotent: present binaries are kept; missing ones are (re)fetched. Each binary is
# fetched independently so one failure can't block the other.
$ErrorActionPreference = "Stop"
# Old PowerShell/.NET defaults to TLS 1.0, which GitHub now refuses -> downloads fail.
# Force TLS 1.2 (the original cause of the "fetch silently did nothing").
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}
$bin = $PSScriptRoot
Write-Host "Fetching Windows host binaries into $bin"

# --- MediaMTX (resolve latest windows_amd64 zip from the GitHub API) ---
$mtxExe = Join-Path $bin "mediamtx.exe"
if (Test-Path $mtxExe) {
    Write-Host "  mediamtx.exe already present - skipping"
} else {
    try {
        $rel = Invoke-RestMethod "https://api.github.com/repos/bluenviron/mediamtx/releases/latest" -Headers @{ "User-Agent" = "discord-ish-steam" }
        $asset = $rel.assets | Where-Object { $_.name -match "windows_amd64\.zip$" } | Select-Object -First 1
        if (-not $asset) { throw "no windows_amd64 asset in the latest MediaMTX release" }
        $zip = Join-Path $env:TEMP $asset.name
        Write-Host "  downloading $($asset.name)"
        Invoke-WebRequest $asset.browser_download_url -OutFile $zip
        Expand-Archive $zip -DestinationPath $bin -Force
        Remove-Item $zip -Force
        if (-not (Test-Path $mtxExe)) { throw "extracted but mediamtx.exe not found" }
        Write-Host "  installed mediamtx.exe"
    } catch {
        Write-Warning "  mediamtx.exe fetch failed ($($_.Exception.Message)) - self-preview + LAN sharing won't work until this succeeds."
    }
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
        Write-Warning "  cloudflared.exe download failed - off-LAN sharing won't work, but LAN sharing will. ($($_.Exception.Message))"
    }
}

# --- final verification (so a partial fetch is visible, not silent) ---
$haveMtx = Test-Path $mtxExe
$haveCf  = Test-Path $cfExe
Write-Host ("Binaries: mediamtx.exe={0}  cloudflared.exe={1}" -f $haveMtx, $haveCf)
if (-not $haveMtx) { Write-Warning "mediamtx.exe still missing - re-run .\bin\fetch-windows.ps1 (check internet / GitHub reachability)." }
Write-Host "Done."
