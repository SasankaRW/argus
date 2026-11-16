# Draws the menu icons like Windows' own menu icons: just the glyph, no background, one Material Symbols Rounded
# outlined glyph (glyphs.txt) filling the icon box with ~1px padding, in two colours (white for the dark theme, #1f1f1f
# for the light one). Each size is rendered from the vector at that size, never scaled down from a bigger one.
#   send-<device>-dark.ico / send-<device>-light.ico: 16/20/24/32/48/256 px; device = phone, calypso, pc
param([Parameter(Mandatory)][string]$Out, [string]$Preview = "")
Add-Type -AssemblyName System.Drawing
New-Item -ItemType Directory -Force "$Out\Assets" | Out-Null
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Weight = "-w500"   # "" = weight 400, "-w500" = weight 500: firmer, reads better at 16 and 20 px
$Glyphs = @{}
foreach ($line in Get-Content (Join-Path $Here "glyphs.txt")) {
    if ($line.Trim()) { $name, $d = $line -split '\|', 2; $Glyphs[$name] = $d }
}

function ParseGlyph([string]$d) {   # path data -> list of figures, each a list of segments (M/L/C with absolute points)
    $t = $d -split ' '; $i = 0; $segs = @()
    while ($i -lt $t.Count) {
        switch ($t[$i]) {
            'M' { $segs += , @('M', [double]$t[$i + 1], [double]$t[$i + 2]); $i += 3 }
            'L' { $segs += , @('L', [double]$t[$i + 1], [double]$t[$i + 2]); $i += 3 }
            'C' { $segs += , @('C', [double]$t[$i + 1], [double]$t[$i + 2], [double]$t[$i + 3], [double]$t[$i + 4], [double]$t[$i + 5], [double]$t[$i + 6]); $i += 7 }
            'Z' { $segs += , @('Z'); $i += 1 }
            default { $i += 1 }
        }
    }
    return , $segs
}

function Bounds($segs) {
    $xs = @(); $ys = @()
    foreach ($g in $segs) { for ($j = 1; $j -lt $g.Count; $j += 2) { $xs += $g[$j]; $ys += $g[$j + 1] } }
    $m = $xs | Measure-Object -Minimum -Maximum; $n = $ys | Measure-Object -Minimum -Maximum
    return @($m.Minimum, $n.Minimum, $m.Maximum, $n.Maximum)
}

function Draw([int]$s, [string]$color, [string]$glyph) {
    $segs = ParseGlyph $Glyphs[$glyph]
    $b = Bounds $segs
    $w = $b[2] - $b[0]; $h = $b[3] - $b[1]
    $pad = [Math]::Max(1, [Math]::Round($s / 16.0))            # ~1px at 16, 2px at 32
    $box = $s - 2 * $pad
    $k = $box / [Math]::Max($w, $h)
    # centre on whole pixels so the edges of the outline land on the pixel grid
    $ox = [Math]::Round(($s - $w * $k) / 2.0) - $b[0] * $k
    $oy = [Math]::Round(($s - $h * $k) / 2.0) - $b[1] * $k
    $gp = New-Object System.Drawing.Drawing2D.GraphicsPath ([System.Drawing.Drawing2D.FillMode]::Winding)
    $cur = $null; $start = $null
    foreach ($g in $segs) {
        switch ($g[0]) {
            'M' { $gp.StartFigure(); $cur = [System.Drawing.PointF]::new([single]($ox + $g[1] * $k), [single]($oy + $g[2] * $k)); $start = $cur }
            'L' { $n = [System.Drawing.PointF]::new([single]($ox + $g[1] * $k), [single]($oy + $g[2] * $k)); $gp.AddLine($cur, $n); $cur = $n }
            'C' { $p1 = [System.Drawing.PointF]::new([single]($ox + $g[1] * $k), [single]($oy + $g[2] * $k))
                  $p2 = [System.Drawing.PointF]::new([single]($ox + $g[3] * $k), [single]($oy + $g[4] * $k))
                  $p3 = [System.Drawing.PointF]::new([single]($ox + $g[5] * $k), [single]($oy + $g[6] * $k))
                  $gp.AddBezier($cur, $p1, $p2, $p3); $cur = $p3 }
            'Z' { $gp.CloseFigure(); $cur = $start }
        }
    }
    $bmp = New-Object System.Drawing.Bitmap $s, $s, ([System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $gr = [System.Drawing.Graphics]::FromImage($bmp)
    $gr.Clear([System.Drawing.Color]::Transparent)
    $gr.SmoothingMode = 'AntiAlias'; $gr.PixelOffsetMode = 'None'   # None: integer coordinates are pixel edges
    $gr.FillPath((New-Object System.Drawing.SolidBrush ([System.Drawing.ColorTranslator]::FromHtml($color))), $gp)
    $gr.Dispose(); return $bmp
}

function SaveIco([string]$file, [string]$color, [string]$glyph) {   # PNG-compressed images in an .ico container
    $sizes = 16, 20, 24, 32, 48, 256
    $pngs = foreach ($s in $sizes) {
        $ms = New-Object System.IO.MemoryStream
        (Draw $s $color $glyph).Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
        , $ms.ToArray()
    }
    $o = New-Object System.IO.MemoryStream; $w = New-Object System.IO.BinaryWriter $o
    $w.Write([uint16]0); $w.Write([uint16]1); $w.Write([uint16]$sizes.Count)
    $pos = 6 + 16 * $sizes.Count
    for ($i = 0; $i -lt $sizes.Count; $i++) {
        $b = [byte]($sizes[$i] % 256)   # 256 is stored as 0
        $w.Write($b); $w.Write($b); $w.Write([byte]0); $w.Write([byte]0)
        $w.Write([uint16]1); $w.Write([uint16]32); $w.Write([uint32]$pngs[$i].Length); $w.Write([uint32]$pos)
        $pos += $pngs[$i].Length
    }
    foreach ($p in $pngs) { $w.Write($p) }
    $w.Flush()
    [System.IO.File]::WriteAllBytes($file, $o.ToArray())
}

if ($Preview) {   # a sheet to look at: rows = weight 400 / 500, per device and theme, at 16/20/24/32 (shown 6x)
    $z = 6; $sizes = 16, 20, 24, 32; $devs = "smartphone", "laptop_chromebook", "computer"
    $sheet = New-Object System.Drawing.Bitmap 1900, 860
    $g = [System.Drawing.Graphics]::FromImage($sheet); $g.InterpolationMode = 'NearestNeighbor'; $g.PixelOffsetMode = 'Half'
    $y = 0
    foreach ($wt in "", "-w500") {
        foreach ($bg in @(@('#2b2b2b', '#ffffff'), @('#f3f3f3', '#1f1f1f'))) {
            $g.FillRectangle((New-Object System.Drawing.SolidBrush ([System.Drawing.ColorTranslator]::FromHtml($bg[0]))), 0, $y, 1900, 215)
            $x = 10
            foreach ($d in $devs) {
                foreach ($s in $sizes) {
                    $script:Weight = $wt
                    $g.DrawImage((Draw $s $bg[1] ($d + $wt)), $x, $y + 10, $s * $z, $s * $z); $x += $s * $z + 8
                }
                $x += 20
            }
            $y += 215
        }
    }
    $g.Dispose(); $sheet.Save($Preview); return
}

foreach ($d in @(@("phone", "smartphone"), @("calypso", "laptop_chromebook"), @("pc", "computer"))) {
    SaveIco "$Out\send-$($d[0])-dark.ico" '#ffffff' ($d[1] + $Weight)
    SaveIco "$Out\send-$($d[0])-light.ico" '#1f1f1f' ($d[1] + $Weight)
}
(Draw 150 '#1f1f1f' ('smartphone' + $Weight)).Save("$Out\Assets\logo.png", [System.Drawing.Imaging.ImageFormat]::Png)
