# Draws the two menu icons (.ico) and the package logo (.png): dark squircle, one tinted glyph, a send arrow.
param([Parameter(Mandatory)][string]$Out)
Add-Type -AssemblyName System.Drawing
New-Item -ItemType Directory -Force "$Out\Assets" | Out-Null
function Draw([int]$s, [string]$tone, [string]$shape) {
    $bmp = New-Object System.Drawing.Bitmap $s, $s
    $g = [System.Drawing.Graphics]::FromImage($bmp); $g.SmoothingMode = 'AntiAlias'
    $d = $s * 0.56
    $path = New-Object System.Drawing.Drawing2D.GraphicsPath
    $path.AddArc(0, 0, $d, $d, 180, 90); $path.AddArc($s - $d, 0, $d, $d, 270, 90)
    $path.AddArc($s - $d, $s - $d, $d, $d, 0, 90); $path.AddArc(0, $s - $d, $d, $d, 90, 90); $path.CloseFigure()
    $g.FillPath((New-Object System.Drawing.SolidBrush ([System.Drawing.ColorTranslator]::FromHtml('#17171c'))), $path)
    $pen = New-Object System.Drawing.Pen ([System.Drawing.ColorTranslator]::FromHtml($tone)), ($s * 0.08)
    $pen.StartCap = 'Round'; $pen.EndCap = 'Round'; $pen.LineJoin = 'Round'
    if ($shape -eq 'phone') { $g.DrawRectangle($pen, $s * 0.3, $s * 0.18, $s * 0.4, $s * 0.64) }
    else { $g.DrawRectangle($pen, $s * 0.2, $s * 0.25, $s * 0.6, $s * 0.4); $g.DrawLine($pen, $s * 0.3, $s * 0.78, $s * 0.7, $s * 0.78) }
    $cy = if ($shape -eq 'phone') { 0.5 } else { 0.45 }
    $g.DrawLine($pen, $s * 0.5, $s * ($cy + 0.12), $s * 0.5, $s * ($cy - 0.12))
    $g.DrawLines($pen, [System.Drawing.PointF[]]@(
        [System.Drawing.PointF]::new($s * 0.4, $s * ($cy - 0.02)), [System.Drawing.PointF]::new($s * 0.5, $s * ($cy - 0.12)),
        [System.Drawing.PointF]::new($s * 0.6, $s * ($cy - 0.02))))
    $g.Dispose(); return $bmp
}
function SaveIco([string]$file, [string]$tone, [string]$shape) {   # one PNG-compressed 64px image in an .ico container
    $ms = New-Object System.IO.MemoryStream
    (Draw 64 $tone $shape).Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
    $png = $ms.ToArray(); $o = New-Object System.IO.MemoryStream; $w = New-Object System.IO.BinaryWriter $o
    $w.Write([uint16]0); $w.Write([uint16]1); $w.Write([uint16]1)
    $w.Write([byte]64); $w.Write([byte]64); $w.Write([byte]0); $w.Write([byte]0)
    $w.Write([uint16]1); $w.Write([uint16]32); $w.Write([uint32]$png.Length); $w.Write([uint32]22); $w.Write($png); $w.Flush()
    [System.IO.File]::WriteAllBytes($file, $o.ToArray())
}
SaveIco "$Out\send-phone.ico" '#a6c8ff' 'phone'
SaveIco "$Out\send-calypso.ico" '#d3b6ff' 'laptop'
(Draw 150 '#a6c8ff' 'phone').Save("$Out\Assets\logo.png", [System.Drawing.Imaging.ImageFormat]::Png)
