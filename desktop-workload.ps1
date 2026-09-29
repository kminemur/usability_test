param([Parameter(Mandatory=$true)][string]$Folder)
$ErrorActionPreference = 'Stop'
$settings = Get-Content -LiteralPath (Join-Path $Folder 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$stopPath = Join-Path $Folder 'stop'
$statusPath = Join-Path $Folder 'status.json'
$eventsPath = Join-Path $Folder 'operations.jsonl'
$excelApp = $null; $book = $null; $presentation = $null
function Report($state, $message, $iteration) {
    @{state=$state; message=$message; iteration=$iteration} | ConvertTo-Json -Compress | Set-Content -LiteralPath $statusPath -Encoding UTF8
}
try {
    Report 'preparing' 'Opening Excel and PowerPoint copies' 0
    $excelApp = New-Object -ComObject Excel.Application
    $excelApp.AutomationSecurity = 3
    $excelApp.DisplayAlerts = $false
    $excelApp.AskToUpdateLinks = $false
    $excelApp.Visible = $true
    $powerpointApp = New-Object -ComObject PowerPoint.Application
    $powerpointApp.AutomationSecurity = 3
    $powerpointApp.Visible = -1
    if ($settings.automatic) {
        $book = $excelApp.Workbooks.Add()
        $sheet = $book.Worksheets.Item(1)
        $sheet.Cells.Item(1,1) = 'ID'; $sheet.Cells.Item(1,2) = 'Sales'; $sheet.Cells.Item(1,3) = 'Tax'
        $sheet.Range('A2:A100001').Formula = '=ROW()'
        $sheet.Range('B2:B100001').Formula = '=MOD(A2*7919,1000000)/100'
        $sheet.Range('C2:C100001').Formula = '=B2*0.1'
        $book.SaveAs((Join-Path $Folder 'workload.xlsx'),51)
        $presentation = $powerpointApp.Presentations.Add()
        Add-Type -AssemblyName System.Drawing
        for ($n=1; $n -le 24; $n++) {
            if (Test-Path -LiteralPath $stopPath) { throw 'Stopped during generation' }
            $bitmap = New-Object Drawing.Bitmap 1920,1080
            $graphics = [Drawing.Graphics]::FromImage($bitmap)
            $graphics.Clear([Drawing.Color]::White)
            for ($j=0; $j -lt 1000; $j++) {
                $brush = New-Object Drawing.SolidBrush ([Drawing.Color]::FromArgb(255,($j*31+$n)%256,($j*17)%256,($j*13)%256))
                $graphics.FillRectangle($brush,($j*37)%1920,($j*71)%1080,90,60)
                $brush.Dispose()
            }
            $picture = Join-Path $Folder ('image-'+$n+'.png')
            $bitmap.Save($picture,[Drawing.Imaging.ImageFormat]::Png)
            $graphics.Dispose(); $bitmap.Dispose()
            $slide=$presentation.Slides.Add($n,12)
            $null=$slide.Shapes.AddPicture($picture,0,-1,0,0,720,405)
            $title=$slide.Shapes.AddTextbox(1,20,20,680,50)
            $title.TextFrame.TextRange.Text='Memory Lab Report '+$n
        }
        $presentation.SaveAs((Join-Path $Folder 'workload.pptx'),24)
        $presentation.SaveCopyAs((Join-Path $Folder 'document-1.pdf'),32)
        Copy-Item -LiteralPath (Join-Path $Folder 'document-1.pdf') -Destination (Join-Path $Folder 'document-2.pdf')
    } else {
        $book = $excelApp.Workbooks.Open($settings.excel, 0, $false)
        $presentation = $powerpointApp.Presentations.Open($settings.powerpoint, 0, 0, -1)
    }
    $sheet = $book.Worksheets.Item(1)
    $range = $sheet.UsedRange
    if ($presentation.Slides.Count -eq 0) { $null = $presentation.Slides.Add(1, 12) }
    $box = $presentation.Slides.Item(1).Shapes.AddTextbox(1, 10, 10, 400, 35)
    $deadline = [DateTime]::UtcNow.AddMinutes(12)
    $iteration = 0
    while (!(Test-Path -LiteralPath $stopPath) -and [DateTime]::UtcNow -lt $deadline) {
        $iteration++
        $watch = [Diagnostics.Stopwatch]::StartNew()
        $excelApp.CalculateFullRebuild()
        $recalc = $watch.Elapsed.TotalMilliseconds
        # First worksheet, used range, first column. First row is treated as a header.
        if ($range.Rows.Count -gt 2) {
            $sheet.Sort.SortFields.Clear()
            $direction = if ($iteration % 2) { 1 } else { 2 }
            $null = $sheet.Sort.SortFields.Add($range.Columns.Item(1), 0, $direction)
            $sheet.Sort.SetRange($range)
            $sheet.Sort.Header = 1
            $sheet.Sort.Apply()
        }
        $sort = $watch.Elapsed.TotalMilliseconds - $recalc
        $box.TextFrame.TextRange.Text = 'Memory Lab iteration ' + $iteration
        $box.Left = 10 + ($iteration % 20)
        $presentation.Windows.Item(1).View.GotoSlide(1 + (($iteration - 1) % $presentation.Slides.Count))
        $presentation.Save()
        @{timestamp=[DateTimeOffset]::Now.ToString('o'); iteration=$iteration; recalc_ms=$recalc; sort_ms=$sort; powerpoint_ms=($watch.Elapsed.TotalMilliseconds-$recalc-$sort)} | ConvertTo-Json -Compress | Add-Content -LiteralPath $eventsPath -Encoding UTF8
        Report 'running' 'Excel recalculation/sort and PowerPoint edit/save completed' $iteration
        for ($i=0; $i -lt 20 -and !(Test-Path -LiteralPath $stopPath); $i++) { Start-Sleep -Milliseconds 250 }
    }
    Report 'stopped' 'Office workload stopped' $iteration
} catch {
    Report 'error' $_.Exception.Message 0
    Write-Error $_ -ErrorAction Continue
    exit 1
} finally {
    if ($book) { try { $book.Close($false) } catch {} }
    if ($excelApp) { try { $excelApp.Quit() } catch {} }
    # PowerPoint may share an existing instance; close only our copied presentation.
    if ($presentation) { try { $presentation.Saved = -1; $presentation.Close() } catch {} }
}
