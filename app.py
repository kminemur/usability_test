"""Local, stage-based system memory monitor. Python 3.9+."""
import argparse
import csv
import json
import secrets
import threading
import time
import webbrowser
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import psutil
from automation import Automation

ROOT = Path(__file__).resolve().parent
STAGES = {'baseline': '開始時', 'tabs': 'タブ追加後', 'tabs5': '資料＋5タブ', 'tabs10': '資料＋10タブ', 'tabs20': '資料＋20タブ', 'tabs30': '資料＋30タブ', 'call': 'Teams通話中', 'camera': 'カメラON', 'share': '画面共有中', 'desktop': 'デスクトップ複合負荷', 'pdf': 'PDF検索中', 'office': '模擬業務・映像OFF', 'stress': 'ブラウザ高負荷', 'recovery': '回復'}
FIELDS = ['timestamp', 'elapsed_s', 'stage', 'system_mib', 'total_mib', 'available_mib', 'memory_percent', 'swap_mib', 'cpu_percent', 'browser_rss_mib', 'teams_rss_mib', 'excel_rss_mib', 'powerpoint_rss_mib', 'pdf_rss_mib', 'unreadable_processes']


def sample():
    memory = psutil.virtual_memory()
    browser = teams = skipped = 0
    native = dict(excel=0, powerpoint=0, pdf=0)
    for process in psutil.process_iter(['name', 'memory_info'], ad_value=None):
        try:
            info = process.info
            name = (info['name'] or '').lower()
            rss = info['memory_info']
            if rss is None:
                skipped += 1
                continue
            if name == 'excel.exe':
                native['excel'] += rss.rss
            elif name == 'powerpnt.exe':
                native['powerpoint'] += rss.rss
            elif name in ('acrobat.exe', 'acrord32.exe', 'sumatrapdf.exe'):
                native['pdf'] += rss.rss
            elif 'teams' in name or 'msteams' in name:
                teams += rss.rss
            elif any(term in name for term in ('chrome', 'chromium', 'msedge', 'microsoft edge', 'firefox', 'safari', 'webkit')):
                browser += rss.rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            skipped += 1
    mib = 1024 ** 2
    return dict(total_mib=memory.total/mib, system_mib=(memory.total-memory.available)/mib,
                available_mib=memory.available/mib, memory_percent=memory.percent,
                swap_mib=psutil.swap_memory().used/mib, cpu_percent=psutil.cpu_percent(),
                browser_rss_mib=browser/mib, teams_rss_mib=teams/mib,
                unreadable_processes=skipped, **{k+'_rss_mib': v/mib for k, v in native.items()})


class Monitor:
    def __init__(self, output):
        self.output = output
        self.lock = threading.RLock()
        self.running = False
        self.stage = 'baseline'
        self.history = deque(maxlen=600)
        self.stats = {}
        self.latest = {}
        self.path = None
        self.error = None
        self.started = 0
        self.done = threading.Event()
        self.automation = Automation(self)

    def start(self):
        with self.lock:
            if self.running or self.automation.status['active']:
                raise ValueError('すでに測定中です。')
            self.output.mkdir(parents=True, exist_ok=True)
            path = self.output / ('measurement_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '.csv')
            with path.open('w', newline='', encoding='utf-8-sig') as f:
                csv.DictWriter(f, fieldnames=FIELDS).writeheader()
            self.path = path
            self.history.clear()
            self.stats.clear()
            self.stage = 'baseline'
            self.started = time.monotonic()
            self.error = None
            self.running = True

    def tick(self):
        # Hold the lock across collection so a stage change cannot mislabel a sample.
        with self.lock:
            self.latest = sample()
            if not self.running:
                return
            row = dict(timestamp=datetime.now().astimezone().isoformat(),
                       elapsed_s=round(time.monotonic()-self.started, 2), stage=self.stage, **self.latest)
            with self.path.open('a', newline='', encoding='utf-8') as f:
                csv.DictWriter(f, fieldnames=FIELDS).writerow(row)
            self.history.append(row)
            stat = self.stats.setdefault(self.stage, {'count': 0, 'sum': 0, 'max': 0, 'browser_sum': 0, 'teams_sum': 0, 'excel_sum': 0, 'powerpoint_sum': 0, 'pdf_sum': 0})
            stat['count'] += 1
            stat['sum'] += row['system_mib']
            stat['max'] = max(stat['max'], row['system_mib'])
            stat['browser_sum'] += row['browser_rss_mib']
            stat['teams_sum'] += row['teams_rss_mib']
            for app in ('excel', 'powerpoint', 'pdf'):
                stat[app+'_sum'] += row.get(app+'_rss_mib', 0)

    def loop(self):
        psutil.cpu_percent()
        while not self.done.wait(1):
            try:
                self.tick()
            except Exception as error:
                with self.lock:
                    self.error = str(error)
                    self.running = False

    def state(self):
        with self.lock:
            summary = []
            base = self.stats.get('baseline')
            baseline = base['sum']/base['count'] if base else None
            for key, label in STAGES.items():
                stat = self.stats.get(key)
                if stat:
                    average = stat['sum']/stat['count']
                    summary.append(dict(stage=label, count=stat['count'], average=average,
                                        maximum=stat['max'], delta=average-baseline if baseline is not None else None,
                                        browser=stat['browser_sum']/stat['count'], teams=stat['teams_sum']/stat['count'],
                                        **{app: stat[app+'_sum']/stat['count'] for app in ('excel', 'powerpoint', 'pdf')}))
            return dict(automation=dict(self.automation.status), running=self.running, stage=self.stage, latest=self.latest,
                        history=list(self.history), summary=summary, error=self.error,
                        file=str(self.path) if self.path else None)


def make_handler(monitor, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, body, mime='application/json; charset=utf-8'):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.headers.get('Host') != '127.0.0.1:' + str(self.server.server_port):
                return self.send(403, {'error': 'Invalid host'})
            if self.path == '/':
                return self.send(200, (ROOT/'index.html').read_text(encoding='utf-8').replace('__TOKEN__', token).encode(), 'text/html; charset=utf-8')
            if self.path == '/api/state':
                return self.send(200, monitor.state())
            if self.path == '/api/csv':
                with monitor.lock:
                    if monitor.path:
                        return self.send(200, monitor.path.read_bytes(), 'text/csv; charset=utf-8')
                return self.send(404, {'error': '測定データがありません。'})
            self.send(404, {'error': 'Not found'})

        def do_POST(self):
            if self.headers.get('X-Token') != token:
                return self.send(403, {'error': 'Invalid token'})
            try:
                length = int(self.headers.get('Content-Length', 0))
                if not 0 < length <= 16384:
                    raise ValueError('リクエストが不正です。')
                data = json.loads(self.rfile.read(length))
                if self.path == '/api/auto':
                    monitor.automation.start(data.get('preset', 'standard'), data.get('desktop'))
                elif self.path == '/api/start':
                    monitor.start()
                elif self.path == '/api/stop':
                    with monitor.lock:
                        monitor.automation.cancel.set()
                        monitor.running = False
                elif self.path == '/api/stage':
                    with monitor.lock:
                        if (monitor.automation.status['active'] and monitor.automation.status.get('preset') != 'desktop') or not monitor.running or data.get('stage') not in STAGES:
                            raise ValueError('測定を開始し、有効な段階を指定してください。')
                        monitor.stage = data['stage']
                elif self.path == '/api/open':
                    urls = data.get('urls')
                    if not isinstance(urls, list) or not 1 <= len(urls) <= 20:
                        raise ValueError('URLは1〜20個指定してください。')
                    for url in urls:
                        if not isinstance(url, str) or urlparse(url).scheme not in ('http', 'https') or not urlparse(url).netloc:
                            raise ValueError('httpまたはhttpsのURLを指定してください。')
                    with ThreadPoolExecutor(max_workers=len(urls)) as pool:
                        opened = list(pool.map(webbrowser.open_new_tab, urls))
                    if not all(opened):
                        raise ValueError('ブラウザを開けませんでした。URLを手動で開いてください。')
                else:
                    return self.send(404, {'error': 'Not found'})
                self.send(200, {'ok': True})
            except (ValueError, TypeError, AttributeError, OSError, webbrowser.Error) as error:
                self.send(400, {'error': str(error)})
    return Handler


def main():
    parser = argparse.ArgumentParser(description='ブラウザ・Teamsの段階別メモリ測定')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--output', type=Path, default=ROOT/'measurements')
    args = parser.parse_args()
    monitor = Monitor(args.output)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(monitor, secrets.token_urlsafe(32)))
    worker = threading.Thread(target=monitor.loop, daemon=True)
    worker.start()
    url = 'http://127.0.0.1:' + str(server.server_port)
    print('Memory Lab: ' + url, flush=True)
    print('終了: Ctrl+C / 保存先: ' + str(args.output), flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        monitor.automation.cancel.set()
        if monitor.automation.thread:
            monitor.automation.thread.join(timeout=25)
        monitor.done.set()
        worker.join(timeout=5)
        server.server_close()


if __name__ == '__main__':
    main()
