"""Windows Office integration check using generated temporary documents only."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='memory-lab-office-') as directory:
    folder = Path(directory)
    setup = folder / 'setup.ps1'
    setup.write_text('''
$ErrorActionPreference='Stop'
$folder=$env:MEMORY_LAB_TEST_DIR
$excel=New-Object -ComObject Excel.Application
$excel.DisplayAlerts=$false
$book=$null; $deck=$null
try {
 $book=$excel.Workbooks.Add()
 $sheet=$book.Worksheets.Item(1)
 $sheet.Cells.Item(1,1)='ID'; $sheet.Cells.Item(1,2)='Value'
 $sheet.Range('A2:A2000').Formula='=ROW()'
 $sheet.Range('B2:B2000').Formula='=A2*37'
 $book.SaveAs((Join-Path $folder 'workload.xlsx'),51)
 $ppt=New-Object -ComObject PowerPoint.Application
 $deck=$ppt.Presentations.Add()
 $slide=$deck.Slides.Add(1,12)
 $shape=$slide.Shapes.AddTextbox(1,20,20,400,100)
 $shape.TextFrame.TextRange.Text='Memory Lab test fixture'
 $deck.SaveAs((Join-Path $folder 'workload.pptx'),24)
} finally {
 if($book){$book.Close($false)}
 $excel.Quit()
 if($deck){$deck.Close()}
}
''', encoding='utf-8-sig')
    env = dict(os.environ, MEMORY_LAB_TEST_DIR=str(folder))
    subprocess.run(['powershell.exe', '-NoProfile', '-File', str(setup)],
                   env=env, check=True, timeout=90, creationflags=subprocess.CREATE_NO_WINDOW)
    (folder / 'config.json').write_text(json.dumps({
        'excel': str(folder / 'workload.xlsx'), 'powerpoint': str(folder / 'workload.pptx')}), encoding='utf-8')
    with (folder / 'worker.log').open('w') as log:
        process = subprocess.Popen(['powershell.exe', '-NoProfile', '-File',
                                    str(ROOT / 'desktop-workload.ps1'), '-Folder', str(folder)],
                                   stdout=log, stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise AssertionError((folder / 'worker.log').read_text(errors='replace'))
                try:
                    status = json.loads((folder / 'status.json').read_text(encoding='utf-8-sig'))
                except (OSError, ValueError):
                    time.sleep(.5)
                    continue
                assert status['state'] != 'error', status
                if status.get('iteration', 0) >= 2:
                    print(status)
                    break
                time.sleep(.5)
            else:
                raise AssertionError('Office workload timed out')
        finally:
            (folder / 'stop').touch()
            process.wait(timeout=30)
    assert process.returncode == 0
    events = (folder / 'operations.jsonl').read_text(encoding='utf-8-sig').splitlines()
    assert len(events) >= 2
    print('Excel recalculation/sort + PowerPoint edit/save + stop: PASS')
