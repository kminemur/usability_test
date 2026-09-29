import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import sample
from desktop import validate


class DesktopTests(unittest.TestCase):
    def test_validation_requires_native_documents_and_multiple_pdfs(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for name in ('book.xlsx', 'deck.pptx', 'a.pdf', 'b.pdf'):
                (folder / name).touch()
            data = dict(excel=str(folder/'book.xlsx'), powerpoint=str(folder/'deck.pptx'),
                        pdfs=[str(folder/'a.pdf'), str(folder/'b.pdf')])
            with patch('desktop.executable', return_value='reader.exe'):
                self.assertEqual(len(validate(data)['pdfs']), 2)
                with self.assertRaises(ValueError):
                    validate(dict(data, pdfs=data['pdfs'][:1]))
                with self.assertRaises(ValueError):
                    validate(dict(data, excel=str(folder/'missing.xlsx')))
                with self.assertRaises(ValueError):
                    validate(dict(data, excel=str(folder/'a.pdf')))

    def test_native_rss_classification(self):
        processes = [SimpleNamespace(info=dict(name=name, memory_info=SimpleNamespace(rss=mib*1024**2)))
                     for name, mib in [('EXCEL.EXE', 10), ('POWERPNT.EXE', 20),
                                       ('Acrobat.exe', 30), ('AcroRd32.exe', 5), ('ms-teams.exe', 8)]]
        with patch('app.psutil.process_iter', return_value=processes):
            result = sample()
        self.assertEqual(result['excel_rss_mib'], 10)
        self.assertEqual(result['powerpoint_rss_mib'], 20)
        self.assertEqual(result['pdf_rss_mib'], 35)
        self.assertEqual(result['teams_rss_mib'], 8)
