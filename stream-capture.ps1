# Windows screen-capture mirror (DRAFT — verify on a real Windows run).
# Mirrors a desktop region into an ffplay window that Steam broadcasts.
# Requires ffmpeg/ffplay on PATH. Args via the non-Steam shortcut launch options:
#   $args[0]  X,Y,W,H   region to capture (Windows virtual-desktop coords)
#   $args[1]  WxH | none  output scale
param([string]$Geom = "0,0,1920,1080", [string]$Scale = "1920x1080")
$p = $Geom -split ','
$vf = @()
if ($Scale -ne 'none') { $vf = @('-vf', "scale=$($Scale -replace 'x', ':')") }
# gdigrab = GDI desktop capture (works everywhere); ddagrab is faster but needs setup.
& ffplay -loglevel error -f gdigrab -framerate 30 `
  -offset_x $p[0] -offset_y $p[1] -video_size "$($p[2])x$($p[3])" -i desktop `
  @vf -noborder -window_title "Steam Stream Capture"
