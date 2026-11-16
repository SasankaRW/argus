# Draws the menu icons and the package logo with the house icon recipe (design/icons/make_icons.py): dark squircle
# #17171c with a faint lit edge, one filled Material Symbols Rounded glyph (glyphs.txt) in one tone, at 54% of the icon.
#   send-phone.ico (smartphone), send-calypso.ico (laptop_chromebook), send-pc.ico (computer): 16/20/24/32/48/256 px
param([Parameter(Mandatory)][string]$Out)
Add-Type -AssemblyName System.Drawing
New-Item -ItemType Directory -Force "$Out\Assets" | Out-Null
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Glyphs = @{}
foreach ($line in Get-Content (Join-Path $Here "glyphs.txt")) {
    if ($line.Trim()) { $name, $d = $line -split '\|', 2; $Glyphs[$name] = $d }
}

function GlyphPath([string]$d, [double]$size, [double]$canvas) {   # path data on a 960 box -> a path centred on the canvas
    $k = $size / 960.0; $off = ($canvas - $size) / 2
    $gp = New-Object System.Drawing.Drawing2D.GraphicsPath ([System.Drawing.Drawing2D.FillMode]::Winding)
    $t = $d -split ' '; $i = 0; $cur = $null; $start = $null
    function P($x, $y) { [System.Drawing.PointF]::new([single]($off + [double]$x * $k), [single]($off + [double]$y * $k)) }
    while ($i -lt $t.Count) {
        switch ($t[$i]) {
            'M' { $gp.StartFigure(); $cur = P $t[$i + 1] $t[$i + 2]; $start = $cur; $i += 3 }
            'L' { $n = P $t[$i + 1] $t[$i + 2]; $gp.AddLine($cur, $n); $cur = $n; $i += 3 }
            'C' { $a = P $t[$i + 1] $t[$i + 2]; $b = P $t[$i + 3] $t[$i + 4]; $e = P $t[$i + 5] $t[$i + 6]
                  $gp.AddBezier($cur, $a, $b, $e); $cur = $e; $i += 7 }
            'Z' { $gp.CloseFigure(); $cur = $start; $i += 1 }
            default { $i += 1 }
        }
    }
    return $gp
}

function Draw([int]$s, [string]$tone, [string]$glyph) {
    $bmp = New-Object System.Drawing.Bitmap $s, $s, ([System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = 'AntiAlias'; $g.PixelOffsetMode = 'HighQuality'
    $d = 2 * 118.0 / 512 * $s
    $sq = New-Object System.Drawing.Drawing2D.GraphicsPath
    $sq.AddArc(0, 0, $d, $d, 180, 90); $sq.AddArc($s - $d, 0, $d, $d, 270, 90)
    $sq.AddArc($s - $d, $s - $d, $d, $d, 0, 90); $sq.AddArc(0, $s - $d, $d, $d, 90, 90); $sq.CloseFigure()
    $g.FillPath((New-Object System.Drawing.SolidBrush ([System.Drawing.ColorTranslator]::FromHtml('#17171c'))), $sq)
    if ($s -ge 32) {
        $edge = New-Object System.Drawing.Pen ([System.Drawing.Color]::FromArgb(18, 255, 255, 255)), ([single]([Math]::Max(1, $s * 2 / 512)))
        $g.DrawPath($edge, $sq)
    }
    $gp = GlyphPath $Glyphs[$glyph] ($s * 0.54) $s
    $g.FillPath((New-Object System.Drawing.SolidBrush ([System.Drawing.ColorTranslator]::FromHtml($tone))), $gp)
    $g.Dispose(); return $bmp
}

function SaveIco([string]$file, [string]$tone, [string]$glyph) {   # PNG-compressed images in an .ico container
    $sizes = 16, 20, 24, 32, 48, 256
    $pngs = foreach ($s in $sizes) {
        $ms = New-Object System.IO.MemoryStream
        (Draw $s $tone $glyph).Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
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

# tones from the Argus palette: primary blue for the phone, tertiary purple for Calypso, secondary for the PC
SaveIco "$Out\send-phone.ico" '#a6c8ff' 'smartphone'
SaveIco "$Out\send-calypso.ico" '#d3b6ff' 'laptop_chromebook'
SaveIco "$Out\send-pc.ico" '#b8c7e8' 'computer'
(Draw 150 '#a6c8ff' 'smartphone').Save("$Out\Assets\logo.png", [System.Drawing.Imaging.ImageFormat]::Png)
