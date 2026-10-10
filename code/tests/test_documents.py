import tempfile,unittest
from email.message import EmailMessage
from pathlib import Path
from invoices_scrapper.documents import parse_message,TextHTML

class DocumentTests(unittest.TestCase):
    def test_alternatives_attachments_and_safe_html(self):
        m=EmailMessage(); m['Subject']='קבלה'; m.set_content('Vendor: Demo\nTotal: 10\n')
        m.add_alternative('<div>חשבונית</div><script>alert(1)</script><img src="https://evil.invalid/pixel"><a href="https://example.invalid/receipt">Receipt</a>',subtype='html')
        m.add_attachment(b'private binary',maintype='application',subtype='octet-stream',filename='../../evil.dat')
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); meta,docs,notes=parse_message(m.as_bytes(),root)
            self.assertEqual(meta['Subject'],'קבלה'); self.assertEqual(len(docs),2)
            safe=(root/'inline-safe.html').read_text(); self.assertNotIn('<script',safe); self.assertNotIn('<img',safe)
            self.assertNotIn('alert(1)',safe); self.assertIn('default-src',safe)
            self.assertTrue((root/'original.eml').exists()); self.assertTrue((root/'links-review.json').exists())
            self.assertIn('external_links_not_fetched_review_links_file',notes)
            self.assertTrue(all(Path(doc.path).parent==root for doc in docs))
    def test_attachment_limit_preserves_bytes(self):
        m=EmailMessage(); m.set_content('Hi'); m.add_attachment(b'123456789',maintype='image',subtype='png',filename='a.png')
        with tempfile.TemporaryDirectory() as d:
            _,docs,_=parse_message(m.as_bytes(),Path(d),max_attachment_bytes=2)
            self.assertEqual(Path(docs[1].path).read_bytes(),b'123456789')
            self.assertIn('attachment_over_processing_size_limit',docs[1].notes)
    def test_script_and_style_hidden(self):
        p=TextHTML(); p.feed('<style>secret</style><p>Visible</p><iframe>hidden</iframe>')
        self.assertEqual(p.text(),'Visible')
