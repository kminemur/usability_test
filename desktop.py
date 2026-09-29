"""Windows desktop workload on disposable copies of user-selected documents."""
import json
import os
import shutil
import subprocess
import hashlib
import zipfile
from urllib.request import Request, urlopen
from pathlib import Path


def executable(name):
    import winreg
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths' + '\\' + name,
                                    0, winreg.KEY_READ | view) as key:
                    value = winreg.QueryValue(key, None).strip('"')
                    if Path(value).is_file():
                        return value
            except OSError:
                pass
    return shutil.which(name)


def validate(data):
    if os.name != 'nt':
        raise ValueError('デスクトップ併用はWindows専用です。')
    if not isinstance(data, dict):
        raise ValueError('資料のパスを指定してください。')
    result = {}
    for key, suffix in (('excel', '.xlsx'), ('powerpoint', '.pptx')):
        raw = data.get(key)
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f'{suffix} ファイルを指定してください。')
        path = Path(raw.strip().strip('"'))
        if not path.is_absolute() or path.suffix.lower() != suffix or not path.is_file():
            raise ValueError(f'{suffix} の存在する絶対パスを指定してください。')
        result[key] = str(path.resolve())
    pdfs = data.get('pdfs')
    if not isinstance(pdfs, list) or not 2 <= len(pdfs) <= 5:
        raise ValueError('PDFは2〜5ファイル指定してください。')
    result['pdfs'] = []
    for raw in pdfs:
        if not isinstance(raw, str):
            raise ValueError('PDFのパスが不正です。')
        path = Path(raw.strip().strip('"'))
        if not path.is_absolute() or path.suffix.lower() != '.pdf' or not path.is_file():
            raise ValueError('PDFの存在する絶対パスを指定してください。')
        result['pdfs'].append(str(path.resolve()))
    for name in ('excel.exe', 'powerpnt.exe'):
        if not executable(name):
            raise ValueError(name + ' が見つかりません。デスクトップ版Officeが必要です。')
    reader = next((p for n in ('Acrobat.exe', 'AcroRd32.exe', 'SumatraPDF.exe')
                   if (p := executable(n))), None)
    if not reader:
        raise ValueError('AcrobatまたはSumatraPDFをインストールしてください。')
    result['reader'] = reader
    return result


def validate_auto():
    if os.name != 'nt':
        raise ValueError('全自動デスクトップはWindows専用です。')
    for name in ('excel.exe', 'powerpnt.exe'):
        if not executable(name):
            raise ValueError(name + ' が必要です。')
    return {'automatic': True}


def portable_reader():
    folder = Path(__file__).with_name('.tools') / 'sumatra'
    target = folder / 'SumatraPDF-3.6.1-64.exe'
    if target.is_file():
        return target
    url = 'https://files.sumatrapdfreader.org/software/sumatrapdf/rel/3.6.1/SumatraPDF-3.6.1-64.zip'
    with urlopen(Request(url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=30) as response:
        data = response.read(20*1024*1024)
    if hashlib.sha256(data).hexdigest() != '98b33a518d42986856d225064b0cd2d3643ecf78cbf84ab873d26cc51877a544':
        raise ValueError('PDFリーダーのダウンロード検証に失敗しました。')
    import io
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        binary = archive.read(target.name)
    folder.mkdir(parents=True, exist_ok=True)
    target.write_bytes(binary)
    return target


class DesktopWorkload:
    def __init__(self, folder):
        self.folder = folder
        self.process = None
        self.log = None
        self.pdf_processes = []
        self.pdf_requests = 0
        self.reader = None

    def start(self, config):
        self.folder.mkdir(parents=True, exist_ok=False)
        paths = {'automatic': config.get('automatic', False)}
        for key in (() if paths['automatic'] else ('excel', 'powerpoint')):
            target = self.folder / ('workload' + Path(config[key]).suffix.lower())
            shutil.copy2(config[key], target)
            paths[key] = str(target.resolve())
        if paths['automatic']:
            self.reader = self.folder / 'SumatraPDF.exe'
            shutil.copy2(portable_reader(), self.reader)
        (self.folder / 'config.json').write_text(json.dumps(paths), encoding='utf-8')
        self.log = (self.folder / 'worker.log').open('w', encoding='utf-8')
        self.process = subprocess.Popen(
            ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
             '-File', str(Path(__file__).with_name('desktop-workload.ps1')),
             '-Folder', str(self.folder.resolve())],
            stdout=self.log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        for path in config.get('pdfs', []):
            subprocess.Popen([config['reader'], path])
        # Opening the client does not join a meeting or share a screen.
        if not paths['automatic']:
            try:
                os.startfile('msteams:')
            except OSError as error:
                (self.folder / 'teams-error.txt').write_text(str(error), encoding='utf-8')

    def search_pdfs(self):
        # Each pass reopens our generated PDFs with a native search command.
        # These are isolated, read-only reader processes, never the user's reader.
        self.close_pdfs()
        for index in (1, 2):
            profile = self.folder / ('pdf-profile-' + str(index))
            profile.mkdir(exist_ok=True)
            (profile / 'SumatraPDF-settings.txt').write_text(
                'ReuseInstance = false\nRememberOpenedFiles = false\n', encoding='utf-8')
            self.pdf_processes.append(subprocess.Popen([
                str(self.reader.resolve()), '-appdata', str(profile.resolve()), '-new-window',
                '-search', 'Memory' if (self.pdf_requests // 2) % 2 == 0 else 'Report',
                str((self.folder / f'document-{index}.pdf').resolve())]))
        self.pdf_requests += 2

    def close_pdfs(self):
        for process in self.pdf_processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
        self.pdf_processes.clear()

    def state(self):
        path = self.folder / 'status.json'
        try:
            return json.loads(path.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            return {'state': 'preparing', 'message': 'Office資料を準備中'}

    def stop(self):
        self.close_pdfs()
        if self.folder.exists():
            (self.folder / 'stop').touch()
        if self.log:
            self.log.close()
