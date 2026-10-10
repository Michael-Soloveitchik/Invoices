import importlib.util,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from invoices_scrapper.html_capture import Sanitizer,capture

class CaptureTests(unittest.TestCase):
    def test_sanitizer_removes_active_elements_and_attributes(self):
        p=Sanitizer();p.feed('<script>secret</script><iframe>bad</iframe><table onclick="bad()"><tr><td style="color:red">Receipt</td></tr></table><img src="file:///etc/passwd"><a href="javascript:bad()">Link</a>')
        value=''.join(p.parts)
        self.assertIn('<table>',value);self.assertIn('color:red',value)
        for blocked in ['secret','iframe','onclick','file:','href','javascript','<script','<img']:self.assertNotIn(blocked,value)
    @unittest.skipUnless(importlib.util.find_spec('weasyprint') and importlib.util.find_spec('fitz'),'optional HTML render packages not installed')
    def test_html_pdf_png_with_all_resource_fetches_denied(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=root/'input.txt';source.write_text('<style>@import url("https://example.invalid/style.css");body{background-image:url("file:///etc/passwd")}</style><h1>Receipt</h1><table><tr><td>Amount</td><td>ILS 25</td></tr></table>',encoding='utf-8')
            with patch('urllib.request.urlopen',side_effect=AssertionError('Network is prohibited')):
                manifest=capture(source,root/'capture')
            self.assertEqual(manifest['capture_kind'],'html_render');self.assertGreaterEqual(manifest['blocked_resources'],1)
            self.assertTrue((root/'capture.pdf').exists());self.assertTrue((root/'capture-1.png').exists())
            self.assertFalse(manifest['resource_fetches_allowed'])
