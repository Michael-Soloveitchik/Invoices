"""Optional isolated HTML email capture. No resource URL is ever fetched.

Uses WeasyPrint's HTML/CSS layout (no JavaScript engine), then PyMuPDF rasterization.
Keep original HTML separately: sanitization and blocked assets change appearance.
"""
from html.parser import HTMLParser
import html,json,sys
from pathlib import Path

class Sanitizer(HTMLParser):
    allowed={'html','head','body','style','title','table','thead','tbody','tfoot','tr','td','th','div','span','p','br','hr','h1','h2','h3','h4','h5','h6','b','strong','i','em','u','small','ul','ol','li','pre','blockquote','center'}
    blocked={'script','iframe','object','embed','svg','math','template','noscript'}
    attributes={'style','class','id','dir','lang','width','height','align','valign','colspan','rowspan','cellpadding','cellspacing','border','bgcolor'}
    def __init__(self): super().__init__(convert_charrefs=True);self.parts=[];self.hidden=0;self.removed=0;self.in_style=False
    def handle_starttag(self,tag,attrs):
        if tag=='embed':self.removed+=1;return
        if tag in self.blocked:self.hidden+=1;self.removed+=1;return
        if self.hidden:return
        if tag not in self.allowed:self.removed+=1;return
        filtered=''.join(f' {k}="{html.escape(v,quote=True)}"' for k,v in attrs if k in self.attributes and v is not None)
        self.parts.append('<'+tag+filtered+'>')
        if tag=='style':self.in_style=True
    def handle_endtag(self,tag):
        if tag in self.blocked:self.hidden=max(0,self.hidden-1);return
        if self.hidden:return
        if tag in self.allowed:self.parts.append('</'+tag+'>')
        if tag=='style':self.in_style=False
    def handle_data(self,data):
        if not self.hidden:self.parts.append(data if self.in_style else html.escape(data))

def capture(source:Path,base:Path,max_pages=30):
    from weasyprint import HTML
    from weasyprint.urls import URLFetcher
    import fitz
    raw=source.read_text(encoding='utf-8')
    if len(raw)>2_000_000:raise ValueError('HTML capture size limit')
    sanitizer=Sanitizer();sanitizer.feed(raw)
    blocked=[]
    class DenyFetcher(URLFetcher):
        def fetch(self,url,headers=None):
            blocked.append(True)
            raise ValueError('All resource fetching disabled')
    deny_fetch=DenyFetcher(allowed_protocols=(),allow_redirects=False,fail_on_errors=False)
    # No base filesystem path is supplied; every CSS/HTML fetch goes through deny_fetch.
    pdf=HTML(string=''.join(sanitizer.parts),url_fetcher=deny_fetch,base_url='https://blocked.invalid/',media_type='screen').write_pdf()
    document=fitz.open(stream=pdf,filetype='pdf')
    if len(document)>max_pages:raise ValueError('HTML capture page limit exceeded')
    base.with_suffix('.pdf').write_bytes(pdf)
    for index,page in enumerate(document,1):
        rect=page.rect
        if rect.width>2000 or rect.height>3000:raise ValueError('HTML page dimensions too large')
        page.get_pixmap(matrix=fitz.Matrix(1.3,1.3),alpha=False).save(str(base.with_name(base.name+f'-{index}.png')))
    manifest={'capture_kind':'html_render','pages':len(document),'removed_elements':sanitizer.removed,'blocked_resources':len(blocked),'javascript_executed':False,'resource_fetches_allowed':False}
    base.with_name(base.name+'-capture.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    document.close();return manifest

if __name__=='__main__':
    capture(Path(sys.argv[1]),Path(sys.argv[2]))
