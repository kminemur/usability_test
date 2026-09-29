"""Bounded browser-only workload in an isolated Chromium instance."""
import json
import math
import threading
import time
from pathlib import Path

import psutil

PRESETS = {
    'business': dict(tabs=4, mib=1, workers=0, duty=0, seconds=30),
    'smoke': dict(tabs=2, mib=16, workers=1, duty=20, seconds=3),
    'standard': dict(tabs=6, mib=96, workers=2, duty=55, seconds=30),
    'heavy': dict(tabs=12, mib=192, workers=8, duty=90, seconds=45),
}


def plan(name):
    if name not in PRESETS:
        raise ValueError('プリセットが不正です。')
    config = dict(PRESETS[name])
    available = psutil.virtual_memory().available // (1024**2)
    reserve = max(768, psutil.virtual_memory().total // (1024**2) // 10)
    if available < reserve + 256:
        raise ValueError('空きメモリが少ないため開始できません。不要なアプリを閉じてください。')
    budget = min(2048, int((available-reserve)*0.5))
    config['mib'] = max(1, min(config['mib'], budget//config['tabs']))
    config['workers'] = min(config['workers'], max(1, (psutil.cpu_count() or 2)-1))
    config['reserve_mib'] = reserve
    return config


class Automation:
    def __init__(self, monitor):
        self.monitor = monitor
        self.cancel = threading.Event()
        self.thread = None
        self.status = {'active': False, 'message': '自動テスト待機中'}

    def start(self, preset):
        with self.monitor.lock:
            if self.status['active'] or self.monitor.running:
                raise ValueError('現在の測定を停止してから開始してください。')
            config = plan(preset)
            self.monitor.start()
            self.cancel.clear()
            self.status = dict(active=True, message='専用ブラウザを準備中', config=config, preset=preset)
            self.thread = threading.Thread(target=self.run, args=(config,), daemon=True)
            self.thread.start()

    def check(self, config):
        if self.cancel.is_set():
            raise InterruptedError('緊急停止しました')
        if psutil.virtual_memory().available/(1024**2) < config['reserve_mib']:
            raise InterruptedError('空きメモリが下限に達したため自動停止しました')
        if self.monitor.error:
            raise InterruptedError('記録エラーのため自動停止しました')
        if time.monotonic() > self.deadline:
            raise InterruptedError('最大実行時間を超えたため自動停止しました')

    def phase(self, stage, config, seconds=None):
        with self.monitor.lock:
            self.monitor.stage = stage
            self.status['message'] = {'baseline':'開始時を測定中', 'tabs':'複数タブを測定中', 'stress':'CPU・メモリ高負荷を測定中', 'recovery':'ブラウザ終了後の回復を測定中'}[stage]
        end = time.monotonic() + (config['seconds'] if seconds is None else seconds)
        while time.monotonic() < end:
            self.check(config)
            self.cancel.wait(0.25)

    def run(self, config):
        message = '自動テスト完了・CSV保存済み'
        self.deadline = time.monotonic() + (360 if self.status.get('preset') == 'business' else 300)
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=False, timeout=20000)
                try:
                    context = browser.new_context()
                    page = context.new_page()
                    page.set_content('<h1>Memory Lab: 自動テスト</h1><p>この専用ブラウザはテスト終了時に自動で閉じます。</p>')
                    self.phase('baseline', config)
                    if self.status.get('preset') == 'business':
                        self.business(context, page, config)
                    else:
                        self.synthetic(context, page, config)
                finally:
                    browser.close()
                self.phase('recovery', config)
        except InterruptedError as error:
            message = str(error) + '・取得済みCSVを保存'
        except Exception as error:
            message = '自動テスト失敗: ' + str(error)[:500]
        finally:
            with self.monitor.lock:
                self.monitor.running = False
                self.status.update(active=False, message=message)
                if self.monitor.path:
                    metadata = self.monitor.path.with_suffix('.json')
                    try:
                        metadata.write_text(json.dumps(dict(self.status, summary=self.monitor.state()['summary']), ensure_ascii=False, indent=2), encoding='utf-8')
                    except OSError as error:
                        self.status['message'] += ' / 設定保存失敗: ' + str(error)

    def synthetic(self, context, page, config):
        pages = [page]
        for _ in range(config['tabs']-1):
            self.check(config)
            pages.append(context.new_page())
        html = Path(__file__).with_name('workload.html').read_text(encoding='utf-8')
        for index, tab in enumerate(pages):
            self.check(config)
            tab.set_default_timeout(5000)
            tab.set_content(html)
            tab.evaluate('(n) => document.title = "Memory Lab / tab " + n', index+1)
        self.phase('tabs', config)
        with self.monitor.lock:
            self.monitor.stage = 'stress'
        remaining = config['workers']
        for tab in pages:
            self.check(config)
            workers = 1 if remaining else 0
            remaining -= workers
            tab.evaluate('(c) => startLoad(c)', dict(mib=config['mib'], workers=workers, duty=config['duty'], duration=(config['seconds']+30)*1000))
            self.cancel.wait(0.4)
        self.phase('stress', config)

    def business(self, context, page, config):
        pages = [page]
        html = Path(__file__).with_name('business.html').read_text(encoding='utf-8')
        for _ in range(config['tabs']-1):
            self.check(config)
            pages.append(context.new_page())
        for tab in pages:
            self.check(config)
            tab.set_default_timeout(10000)
            tab.set_content(html)
        self.phase('tabs', config)
        latency_path = self.monitor.path.with_suffix('.latency.jsonl')
        measurements = []
        def record(tab, phase, index):
            self.check(config)
            tab.bring_to_front()
            result = tab.evaluate('() => Promise.race([businessStep(), new Promise((_,reject)=>setTimeout(()=>reject(new Error("操作タイムアウト")),8000))])')
            result.update(phase=phase, tab=index, elapsed_s=round(time.monotonic()-self.monitor.started,2))
            with latency_path.open('a', encoding='utf-8') as f:
                f.write(json.dumps(result)+'\n')
            measurements.append(result)
        # Measure the same foreground operations with video OFF before adding video.
        with self.monitor.lock:
            self.monitor.stage = 'office'
            self.status['message'] = '模擬業務：映像OFFで検索・並べ替え・描画を測定中'
        end = time.monotonic()+30
        while time.monotonic()<end:
            for index, tab in enumerate(pages):
                record(tab, 'office', index)
                self.cancel.wait(0.3)
        for tab in pages:
            self.check(config)
            tab.evaluate('() => startBusiness()')
        with self.monitor.lock:
            self.monitor.stage = 'stress'
            self.status['message'] = '模擬業務：映像＋表編集＋資料スクロールを測定中（150秒）'
        end = time.monotonic()+150
        while time.monotonic()<end:
            for index, tab in enumerate(pages):
                record(tab, 'stress', index)
                self.cancel.wait(0.3)
        results = {}
        for phase in ('office','stress'):
            subset = [r for r in measurements if r['phase']==phase]
            values = sorted(r['paint_ms'] for r in subset)
            results[phase] = dict(count=len(values), mean_ms=sum(values)/len(values),
                                 p95_ms=values[max(0, math.ceil(len(values)*0.95)-1)],
                                 max_ms=max(values), video_frames_max=max(r['video_frames'] for r in subset))
        with self.monitor.lock:
            self.status['latency'] = results
