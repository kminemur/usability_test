import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from app import Monitor, sample


class MonitorTests(unittest.TestCase):
    def test_dashboard_served_as_utf8(self):
        import threading
        from http.server import ThreadingHTTPServer
        from urllib.request import urlopen
        from app import make_handler
        with tempfile.TemporaryDirectory() as directory:
            monitor = Monitor(Path(directory))
            server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(monitor, 'test-token'))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with urlopen(f'http://127.0.0.1:{server.server_port}/', timeout=5) as response:
                    html = response.read().decode('utf-8')
                    self.assertEqual(response.status, 200)
                    self.assertIn('設定・手動操作', html)
                    self.assertIn('test-token', html)
                    self.assertNotIn('__TOKEN__', html)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_stages_export_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            m = Monitor(Path(directory))
            m.start()
            row = dict(system_mib=100, available_mib=500, memory_percent=20,
                       swap_mib=0, cpu_percent=10, browser_rss_mib=30,
                       teams_rss_mib=0, unreadable_processes=0)
            with patch('app.sample', return_value=row):
                m.tick()
                m.stage = 'call'
                m.tick()
                row['system_mib'] = 200
                m.tick()
            state = m.state()
            self.assertEqual(state['summary'][1]['average'], 150)
            self.assertEqual(state['summary'][1]['delta'], 50)
            self.assertEqual(state['summary'][1]['maximum'], 200)
            old = m.path
            with old.open(encoding='utf-8-sig') as f:
                saved = list(csv.DictReader(f))
            self.assertEqual([r['stage'] for r in saved], ['baseline', 'call', 'call'])
            m.running = False
            m.tick()
            self.assertEqual(len(m.history), 3)
            m.start()
            self.assertTrue(old.exists())
            self.assertNotEqual(old, m.path)
            self.assertEqual(m.state()['summary'], [])

    def test_live_sample(self):
        row = sample()
        self.assertGreater(row['total_mib'], 0)
        self.assertGreater(row['system_mib'], 0)
        self.assertGreaterEqual(row['available_mib'], 0)
        self.assertGreaterEqual(row['memory_percent'], 0)
        self.assertLessEqual(row['memory_percent'], 100)

    def test_practical_stages_csv(self):
        from app import STAGES
        stages = ['tabs5', 'tabs10', 'tabs20', 'tabs30', 'share']
        with tempfile.TemporaryDirectory() as directory:
            monitor = Monitor(Path(directory))
            monitor.start()
            with patch('app.sample', return_value=dict(
                    system_mib=1024, total_mib=8192, browser_rss_mib=100,
                    teams_rss_mib=0)):
                for stage in stages:
                    self.assertIn(stage, STAGES)
                    monitor.stage = stage
                    monitor.tick()
            monitor.running = False
            with monitor.path.open(encoding='utf-8-sig') as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual([row['stage'] for row in rows], stages)
            self.assertTrue(all(row['total_mib'] == '8192' for row in rows))
            self.assertEqual(len(monitor.state()['summary']), len(stages))


if __name__ == '__main__':
    unittest.main()

class AutomationTests(unittest.TestCase):
    def test_tabs_load_concurrently(self):
        import asyncio
        from unittest.mock import AsyncMock, Mock

        async def exercise():
            started = 0
            ready = asyncio.Event()

            async def load(html):
                nonlocal started
                started += 1
                if started == 4:
                    ready.set()
                await asyncio.wait_for(ready.wait(), timeout=1)

            pages = [Mock(set_content=AsyncMock(side_effect=load),
                          evaluate=AsyncMock()) for _ in range(4)]
            context = Mock(new_page=AsyncMock(side_effect=pages[1:]))
            with tempfile.TemporaryDirectory() as directory:
                automation = Monitor(Path(directory)).automation
                with patch.object(automation, 'check'):
                    result = await automation.open_tabs(
                        context, pages[0], {'tabs': 4}, 'business.html')
            self.assertEqual(result, pages)
            self.assertEqual(started, 4)

        asyncio.run(exercise())

    def test_memory_budget_and_invalid_preset(self):
        from automation import plan
        from types import SimpleNamespace
        with patch('automation.psutil.virtual_memory', return_value=SimpleNamespace(available=2*1024**3, total=8*1024**3)):
            config = plan('heavy')
            self.assertLessEqual(config['mib']*config['tabs'], (2048-config['reserve_mib'])*0.5)
        with patch('automation.psutil.virtual_memory', return_value=SimpleNamespace(available=512*1024**2, total=8*1024**3)):
            with self.assertRaises(ValueError):
                plan('heavy')
        with self.assertRaises(ValueError):
            plan('unknown')

    def test_cancel_and_restart_guard(self):
        import time
        with tempfile.TemporaryDirectory() as directory:
            m = Monitor(Path(directory))
            m.automation.status['active'] = True
            with self.assertRaises(ValueError):
                m.start()
            m.automation.deadline = time.monotonic()+30
            m.automation.cancel.set()
            with self.assertRaises(InterruptedError):
                m.automation.check({'reserve_mib': 1})
