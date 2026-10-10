import base64,tempfile,unittest
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from invoices_scrapper.scope import epoch,ScopedSource
from invoices_scrapper.sources import RawMessage,GmailSource
from invoices_scrapper.pipeline import run

def message(ident,date):
 m=EmailMessage();m['Date']=date;m.set_content('demo');return RawMessage(ident,m.as_bytes())
class ScopeTests(unittest.TestCase):
 def test_timezone_is_required_and_window_order_validated(self):
  with self.assertRaises(ValueError):epoch('2026-08-10')
  with self.assertRaises(ValueError):ScopedSource(None,'2026-10-01T00:00:00Z','2026-08-01T00:00:00Z')
  self.assertEqual(epoch('2026-08-10T00:00:00+03:00'),epoch('2026-08-09T21:00:00Z'))
 def test_start_inclusive_end_exclusive_before_extraction(self):
  messages=[message('before','Sun, 09 Aug 2026 20:59:59 +0000'),message('start','Sun, 09 Aug 2026 21:00:00 +0000'),message('end','Sat, 10 Oct 2026 21:00:00 +0000')]
  source=ScopedSource(SimpleNamespace(messages=lambda:iter(messages)),'2026-08-10T00:00:00+03:00','2026-10-11T00:00:00+03:00')
  with tempfile.TemporaryDirectory() as d,patch('invoices_scrapper.pipeline.extract',return_value=[]) as model:
   counts=run(source,Path(d),{});self.assertEqual(model.call_count,1);self.assertEqual(counts['filtered_out'],2);self.assertTrue(counts['scan_complete'])
 def test_missing_date_not_sent_and_scan_incomplete(self):
  source=ScopedSource(SimpleNamespace(messages=lambda:iter([RawMessage('bad',b'Subject: hi\n\nPrivate')])),start='2026-01-01T00:00:00Z')
  with tempfile.TemporaryDirectory() as d,patch('invoices_scrapper.pipeline.extract') as model:
   counts=run(source,Path(d),{});model.assert_not_called();self.assertFalse(counts['scan_complete']);self.assertEqual(counts['failed'],1)
 def test_sample_limit_reports_incomplete(self):
  source=ScopedSource(SimpleNamespace(messages=lambda:iter([RawMessage('one',b'\none'),RawMessage('two',b'\ntwo')])),max_messages=1)
  with tempfile.TemporaryDirectory() as d,patch('invoices_scrapper.pipeline.extract',return_value=[]):
   counts=run(source,Path(d),{});self.assertTrue(counts['sample_truncated']);self.assertFalse(counts['scan_complete']);self.assertEqual(counts['processed'],1)
 def test_gmail_epoch_query_and_received_timestamp(self):
  source=GmailSource('test-token',start_epoch=100,end_epoch=200)
  calls=[]
  def get(path,params=None):
   calls.append((path,params))
   if path=='/profile':return {'emailAddress':'demo@example.invalid'}
   if path=='/messages':return {'messages':[{'id':'a'}]}
   return {'raw':base64.urlsafe_b64encode(b'\nhello').decode(),'internalDate':'150000'}
  with patch.object(source,'_get',side_effect=get):result=list(source.messages())
  self.assertEqual(calls[1][1]['q'],'after:99 before:200');self.assertEqual(result[0].received_at,150)
