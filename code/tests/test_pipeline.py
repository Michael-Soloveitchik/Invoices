import csv,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from email.message import EmailMessage
from invoices_scrapper.pipeline import run,cell,annotate
from invoices_scrapper.documents import Document

def record(**updates):
    result=dict(vendor='Supplier',invoice_number='INV1',date='2026-01-01',due_date='',currency='ILS',total='100.00',amount_paid='100.00',amount_due='0.00',status='paid',kind='receipt',confidence=0.95,evidence='test')
    result.update(updates); return result

def source(*names):
    messages=[]
    for name in names:
        m=EmailMessage(); m.set_content('test'); m['Message-ID']=name
        messages.append(SimpleNamespace(source_id=name,raw=m.as_bytes(),url=''))
    return SimpleNamespace(messages=lambda:iter(messages))

class PipelineTests(unittest.TestCase):
    def test_csv_formula_prevention(self):
        for value in ['=cmd()', ' +evil','\t-1','@sum(A1)']: self.assertTrue(cell(value).startswith("'"))
        self.assertEqual(cell('שלום'),'שלום')
    def test_resume_and_evidence_preservation(self):
        with tempfile.TemporaryDirectory() as d,patch('invoices_scrapper.pipeline.extract',return_value=[record()]) as extract:
            out=Path(d); first=run(source('one','two'),out,{})
            self.assertEqual(first['evidence_rows'],2); self.assertEqual(first['consumptions'],1)
            second=run(source('one','two'),out,{})
            self.assertEqual(second['skipped'],2); self.assertEqual(extract.call_count,2)
            with (out/'reports'/'evidence.csv').open(encoding='utf-8-sig') as f: rows=list(csv.DictReader(f))
            self.assertEqual({r['source_id'] for r in rows},{'one','two'})
            self.assertEqual((out/'reports'/'evidence.csv').read_bytes()[:3],b'\xef\xbb\xbf')
    def test_same_vendor_not_merge_different_invoice(self):
        d=Document('abc','doc','test','text/plain'); s=SimpleNamespace(source_id='x',url='')
        a=annotate(record(),d,0,s,{},{}); b=annotate(record(invoice_number='INV2'),d,1,s,{},{})
        self.assertNotEqual(a['id_consumption'],b['id_consumption']); self.assertEqual(a['supplier_id'],b['supplier_id'])
    def test_explicit_invoice_reference_links_receipt(self):
        d=Document('abc','doc','test','text/plain'); s=SimpleNamespace(source_id='x',url='')
        a=annotate(record(),d,0,s,{},{}); b=annotate(record(invoice_number='',related_invoice_number='INV1',document_number='R88'),d,1,s,{},{})
        self.assertEqual(a['id_consumption'],b['id_consumption'])
    def test_failure_retried(self):
        with tempfile.TemporaryDirectory() as d:
            with patch('invoices_scrapper.pipeline.extract',side_effect=ValueError('PRIVATE TOKEN')):
                result=run(source('one'),Path(d),{}); self.assertEqual(result['failed'],1)
            self.assertNotIn('PRIVATE TOKEN',(Path(d)/'reports'/'review.csv').read_text())
            with patch('invoices_scrapper.pipeline.extract',return_value=[record()]):
                result=run(source('one'),Path(d),{}); self.assertEqual(result['processed'],1)
    def test_conflict_and_ambiguous_duplicates_excluded(self):
        with tempfile.TemporaryDirectory() as d,patch('invoices_scrapper.pipeline.extract',side_effect=[[record()],[record(total='50.00',status='partial')],[record(invoice_number='INV2')]]):
            result=run(source('one','two','three'),Path(d),{})
            with (Path(d)/'reports'/'consumptions.csv').open(encoding='utf-8-sig') as f: rows=list(csv.DictReader(f))
            self.assertTrue(all(r['include_in_totals']=='no' for r in rows))
    def test_source_failure_reported(self):
        def broken():
            raise OSError('offline')
            yield
        with tempfile.TemporaryDirectory() as d:
            result=run(SimpleNamespace(messages=broken),Path(d),{})
            self.assertFalse(result['scan_complete']); self.assertEqual(result['failed'],1)
    def test_reprocess_keeps_old_content_version(self):
        with tempfile.TemporaryDirectory() as d:
            first=source('same'); first.messages().__next__().raw # Synthetic sanity.
            with patch('invoices_scrapper.pipeline.extract',return_value=[record()]): run(first,Path(d),{})
            with (Path(d)/'reports'/'evidence.csv').open(encoding='utf-8-sig') as f: old=list(csv.DictReader(f))[0]['document_path']
            before=Path(old).read_bytes()
            m=EmailMessage();m.set_content('NEW ORIGINAL');second=SimpleNamespace(messages=lambda:iter([SimpleNamespace(source_id='same',raw=m.as_bytes(),url='')]))
            with patch('invoices_scrapper.pipeline.extract',side_effect=ValueError('fail')): run(second,Path(d),{})
            self.assertEqual(Path(old).read_bytes(),before)
            with (Path(d)/'reports'/'expenses.csv').open(encoding='utf-8-sig') as f: row=list(csv.DictReader(f))[0]
            self.assertEqual(row['countable'],'no')
    def test_repeated_layout_without_identifiers_or_dates_reviewed(self):
        a=record(invoice_number='',date='',status='unknown',evidence='Desktop receipt')
        b=record(invoice_number='',date='',status='unknown',evidence='Mobile receipt')
        with tempfile.TemporaryDirectory() as d,patch('invoices_scrapper.pipeline.extract',return_value=[a,b]):
            run(source('one'),Path(d),{})
            with (Path(d)/'reports'/'expenses.csv').open(encoding='utf-8-sig') as f: rows=list(csv.DictReader(f))
            self.assertEqual(len(rows),2);self.assertTrue(all(r['countable']=='no' for r in rows))
    def test_archive_date_and_mandatory_png(self):
        with tempfile.TemporaryDirectory() as d,patch('invoices_scrapper.pipeline.extract',return_value=[record()]):
            run(source('one'),Path(d),{'render_formats':[]})
            with (Path(d)/'reports'/'documents.csv').open(encoding='utf-8-sig') as f: row=list(csv.DictReader(f))[0]
            self.assertIn('/2026/01/',row['file_path'])
            self.assertTrue(list(Path(row['file_path']).parent.glob('inline-*.png')))
    def test_missing_mandatory_png_remains_retryable(self):
        with tempfile.TemporaryDirectory() as d,patch('invoices_scrapper.documents.render_inline',return_value=['png_render_failed']),patch('invoices_scrapper.pipeline.extract') as model:
            counts=run(source('one'),Path(d),{})
            self.assertEqual(counts['failed'],1);self.assertEqual(counts['processed'],0);model.assert_not_called()
    def test_archive_root_reports_and_month_scaffolding(self):
        from datetime import datetime,timezone
        with tempfile.TemporaryDirectory() as d,patch('invoices_scrapper.pipeline.extract',return_value=[]):
            root=Path(d)/'Invoices';run(source('one'),root,{})
            self.assertTrue((root/'reports'/'expenses.csv').is_file());self.assertFalse((root/'documents').exists())
            now=datetime.now(timezone.utc)
            for offset in range(25):
                year,month=divmod(now.year*12+now.month-1-offset,12)
                self.assertTrue((root/f'{year:04d}'/f'{month+1:02d}').is_dir())
