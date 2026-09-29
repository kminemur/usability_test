"""Bounded browser-only workload in an isolated Chromium instance."""
import asyncio
import base64
import json
import math
import threading
import time
from pathlib import Path

import psutil

PRESETS = {
    'desktop_auto': dict(tabs=2, mib=1, workers=0, duty=0, seconds=10),
    'desktop': dict(tabs=2, mib=1, workers=0, duty=0, seconds=10),
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

    def start(self, preset, desktop=None):
        with self.monitor.lock:
            if self.status['active'] or self.monitor.running:
                raise ValueError('現在の測定を停止してから開始してください。')
            config = plan(preset)
            if preset == 'desktop_auto':
                from desktop import validate_auto
                config['desktop'] = validate_auto()
            if preset == 'desktop':
                from desktop import validate
                config['desktop'] = validate(desktop)
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

    async def phase(self, stage, config, seconds=None):
        with self.monitor.lock:
            self.monitor.stage = stage
            self.status['message'] = {'baseline':'開始時を測定中', 'tabs':'複数タブを測定中', 'stress':'CPU・メモリ高負荷を測定中', 'recovery':'ブラウザ終了後の回復を測定中'}[stage]
        end = time.monotonic() + (config['seconds'] if seconds is None else seconds)
        while time.monotonic() < end:
            self.check(config)
            await asyncio.sleep(0.25)

    def run(self, config):
        asyncio.run(self.run_async(config))

    async def run_async(self, config):
        message = '自動テスト完了・CSV保存済み'
        self.deadline = time.monotonic() + (780 if self.status.get('preset') in ('desktop', 'desktop_auto') else 360 if self.status.get('preset') == 'business' else 300)
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=False, timeout=20000, args=['--disable-background-timer-throttling', '--disable-renderer-backgrounding', '--disable-backgrounding-occluded-windows'])
                try:
                    context = await browser.new_context()
                    page = await context.new_page()
                    await page.set_content('<h1>Memory Lab: 自動テスト</h1><p>この専用ブラウザはテスト終了時に自動で閉じます。</p>')
                    await self.phase('baseline', config)
                    if self.status.get('preset') in ('desktop', 'desktop_auto'):
                        await self.desktop(context, page, config)
                    elif self.status.get('preset') == 'business':
                        await self.business(context, page, config)
                    else:
                        await self.synthetic(context, page, config)
                finally:
                    await browser.close()
                await self.phase('recovery', config)
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

    async def open_tabs(self, context, page, config, filename):
        self.check(config)
        pages = [page, *await asyncio.gather(
            *(context.new_page() for _ in range(config['tabs'] - 1)))]
        html = Path(__file__).with_name(filename).read_text(encoding='utf-8')

        if filename == 'business.html':
            video = base64.b64encode(Path(__file__).with_name('workload-video.webm').read_bytes()).decode('ascii')
            html = html.replace('__MOVIE_URL__', 'data:video/webm;base64,' + video)

        async def load(tab, index):
            self.check(config)
            tab.set_default_timeout(10000)
            await tab.set_content(html)
            await tab.evaluate('(n) => document.title = "Memory Lab / tab " + n', index + 1)

        await asyncio.gather(*(load(tab, index) for index, tab in enumerate(pages)))
        self.check(config)
        return pages

    async def synthetic(self, context, page, config):
        pages = await self.open_tabs(context, page, config, 'workload.html')
        await self.phase('tabs', config)
        with self.monitor.lock:
            self.monitor.stage = 'stress'
        remaining = config['workers']
        for tab in pages:
            self.check(config)
            workers = 1 if remaining else 0
            remaining -= workers
            await tab.evaluate('(c) => startLoad(c)', dict(mib=config['mib'], workers=workers, duty=config['duty'], duration=(config['seconds']+30)*1000))
        await self.phase('stress', config)

    async def business(self, context, page, config):
        pages = await self.open_tabs(context, page, config, 'business.html')
        await self.phase('tabs', config)
        latency_path = self.monitor.path.with_suffix('.latency.jsonl')
        measurements = []
        async def record(tab, phase, index):
            self.check(config)
            await tab.bring_to_front()
            result = await tab.evaluate('() => Promise.race([businessStep(), new Promise((_,reject)=>setTimeout(()=>reject(new Error("操作タイムアウト")),8000))])')
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
                await record(tab, 'office', index)
                await asyncio.sleep(0.3)
        self.check(config)
        await asyncio.wait_for(asyncio.gather(*(tab.evaluate('() => startBusiness()') for tab in pages)), timeout=30)
        self.check(config)
        with self.monitor.lock:
            self.monitor.stage = 'stress'
            self.status['message'] = '模擬業務：動画＋経路検索＋WebRTC会議＋資料操作を測定中（150秒）'
        end = time.monotonic()+150
        while time.monotonic()<end:
            for index, tab in enumerate(pages):
                await record(tab, 'stress', index)
                await asyncio.sleep(0.3)
        results = {}
        for phase in ('office','stress'):
            subset = [r for r in measurements if r['phase']==phase]
            values = sorted(r['paint_ms'] for r in subset)
            results[phase] = dict(count=len(values), mean_ms=sum(values)/len(values),
                                 p95_ms=values[max(0, math.ceil(len(values)*0.95)-1)],
                                 max_ms=max(values), video_frames_max=max(r['video_frames'] for r in subset))
        with self.monitor.lock:
            self.status['latency'] = results

    async def desktop(self, context, page, config):
        from desktop import DesktopWorkload
        worker = DesktopWorkload(self.monitor.path.with_suffix('.desktop'))
        try:
            automatic = config['desktop'].get('automatic', False)
            await asyncio.to_thread(worker.start, config['desktop'])
            pages = await self.open_tabs(context, page, config, 'business.html')
            await asyncio.wait_for(asyncio.gather(*(tab.evaluate('(conference) => startBusiness(conference,660000)', automatic) for tab in pages)), 30)
            with self.monitor.lock:
                self.monitor.stage = 'desktop'
                self.status['message'] = '試験資料を生成中（全自動・会議はローカル模擬）' if automatic else 'Office準備中。PDF検索・Teams参加・画面共有は手動で行ってください。'
            end = time.monotonic() + 600
            prepare_deadline = time.monotonic() + 180
            next_pdf_search = 0
            latency = self.monitor.path.with_suffix('.latency.jsonl')
            while time.monotonic() < end:
                self.check(config)
                state = worker.state()
                if state['state'] == 'preparing' and time.monotonic() > prepare_deadline:
                    raise RuntimeError('Office準備が180秒以内に完了しませんでした。ダイアログや保護ビューを確認してください。')
                if state['state'] == 'error':
                    raise RuntimeError('Office: ' + state['message'])
                if worker.process.poll() is not None:
                    raise RuntimeError('Office処理が終了しました: ' + worker.state().get('message', '') + ' / .desktop/worker.logを確認してください。')
                if automatic and any(p.poll() is not None for p in worker.pdf_processes):
                    raise RuntimeError('専用PDFリーダーが終了しました。PDF検索を継続できません。')
                if automatic and state['state'] == 'running' and time.monotonic() >= next_pdf_search:
                    await asyncio.to_thread(worker.search_pdfs)
                    next_pdf_search = time.monotonic() + 15
                with self.monitor.lock:
                    self.status['desktop'] = state
                    self.status['pdf_search_requests'] = worker.pdf_requests
                    self.status['message'] = ('Office操作 ' + str(state.get('iteration', 0)) +
                        ('回 / PDF検索要求 ' + str(worker.pdf_requests) + '件・動画・地図・模擬会議。残り約' if automatic else '回 / 動画・地図継続中。PDF検索・Teams会議・画面共有は手動。残り約') +
                        str(max(0, int(end-time.monotonic()))) + '秒')
                for index, tab in enumerate(pages):
                    result = await tab.evaluate('() => workloadMetrics()')
                    result.update(phase=self.monitor.stage, tab=index, elapsed_s=round(time.monotonic()-self.monitor.started, 2))
                    with latency.open('a', encoding='utf-8') as stream:
                        stream.write(json.dumps(result)+'\n')
                await asyncio.sleep(1)
        finally:
            worker.stop()
            if worker.process:
                for _ in range(40):
                    if worker.process.poll() is not None:
                        break
                    await asyncio.sleep(0.25)
                if worker.process.poll() is None:
                    with self.monitor.lock:
                        self.status['desktop_cleanup'] = 'Office操作の終了待ちです。コピーの資料だけを手動で閉じてください。'
