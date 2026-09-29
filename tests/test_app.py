import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from app import Monitor, sample


class MonitorTests(unittest.TestCase):
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
        self.assertGreater(row['system_mib'], 0)
        self.assertGreaterEqual(row['available_mib'], 0)
        self.assertGreaterEqual(row['memory_percent'], 0)
        self.assertLessEqual(row['memory_percent'], 100)


if __name__ == '__main__':
    unittest.main()

class AutomationTests(unittest.TestCase):
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
