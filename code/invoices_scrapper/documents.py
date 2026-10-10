"""Offline MIME preservation and text extraction. Never follows receipt links."""
from __future__ import annotations
import hashlib, html, json, re, shutil, subprocess, tempfile, sys
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path

@dataclass
class Document:
    id: str
    path: str
    text: str
    mime: str
    notes: list[str] = field(default_factory=list)
    capture_kind: str = ''

class TextHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True); self.parts=[]; self.hidden=0; self.links=[]
    def handle_starttag(self, tag, attrs):
        if tag in {'script','style','iframe','object','template'}: self.hidden += 1
        if tag in {'p','br','div','tr','li','h1','h2','table'} and not self.hidden: self.parts.append('\n')
        if tag == 'a':
            for k,v in attrs:
                if k == 'href' and v and re.match(r'^https?://',v,re.I): self.links.append(v)
    def handle_endtag(self, tag):
        if tag in {'script','style','iframe','object','template'}: self.hidden=max(0,self.hidden-1)
        if tag in {'td','th'} and not self.hidden: self.parts.append(' | ')
    def handle_data(self, data):
        if not self.hidden: self.parts.append(data)
    def text(self): return re.sub(r'\n[ \t]*\n+', '\n\n', ''.join(self.parts)).strip()

def digest(data: bytes) -> str: return hashlib.sha256(data).hexdigest()

def command(args, *, timeout=90):
    result=subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=True)
    return result.stdout.decode('utf-8', errors='replace')

def binary_text(path: Path, mime: str, ocr: bool, max_pages: int) -> tuple[str,list[str]]:
    notes=[]; text=''
    if mime == 'application/pdf' or path.suffix.lower()=='.pdf':
        if shutil.which('pdftotext'):
            try: text=command(['pdftotext','-layout','-f','1','-l',str(max_pages),str(path),'-'])
            except (subprocess.SubprocessError,OSError): notes.append('pdf_text_extraction_failed')
        else: notes.append('pdftotext_not_installed')
        if shutil.which('pdfinfo'):
            try:
                info=command(['pdfinfo',str(path)])
                pages=re.search(r'^Pages:\s*(\d+)',info,re.M)
                if not pages: notes.append('pdf_page_count_unverified')
                elif int(pages.group(1))>max_pages: notes.append(f'pdf_truncated_at_page_limit_{max_pages}')
            except (subprocess.SubprocessError,OSError): notes.append('pdf_page_count_unverified')
        else: notes.append('pdf_page_count_unverified')
        if ocr and len(text.strip())<30:
            if shutil.which('pdftoppm') and shutil.which('tesseract'):
                try:
                    with tempfile.TemporaryDirectory() as tmp:
                        prefix=str(Path(tmp)/'page')
                        command(['pdftoppm','-r','130','-f','1','-l',str(max_pages),'-png',str(path),prefix])
                        text='\n'.join(command(['tesseract',str(p),'stdout','-l','eng+heb']) for p in sorted(Path(tmp).glob('page-*.png')))
                except (subprocess.SubprocessError,OSError): notes.append('ocr_failed_check_eng_heb_language_packs')
            else: notes.append('ocr_tools_not_installed')
    elif mime.startswith('image/'):
        if ocr and shutil.which('tesseract'):
            try: text=command(['tesseract',str(path),'stdout','-l','eng+heb'])
            except (subprocess.SubprocessError,OSError): notes.append('ocr_failed_check_eng_heb_language_packs')
        else: notes.append('image_requires_ocr')
    else: notes.append('unsupported_attachment_type')
    if not text.strip(): notes.append('no_extractable_text')
    return text,notes

def render_inline(text: str, base: Path, formats: list[str]) -> list[str]:
    """Reconstruct text only; no browser, network, scripts, CSS or remote fonts."""
    notes=[]
    if 'png' in formats:
        try:
            from PIL import Image,ImageDraw,ImageFont
            font=ImageFont.truetype('DejaVuSans.ttf',18)
            # Keep all lines by splitting over multiple bounded pages, not cropping.
            lines=[]
            for line in text.splitlines():
                buffer=''
                for character in line:
                    if font.getlength(buffer+character)>1250 and buffer:
                        lines.append(buffer); buffer=''
                    buffer+=character
                lines.append(buffer)
            for n,start in enumerate(range(0,max(1,len(lines)),65),1):
                page=lines[start:start+65]; im=Image.new('RGB',(1300, max(120,40+len(page)*26)),'white')
                draw=ImageDraw.Draw(im)
                for y,line in enumerate(page): draw.text((20,20+y*26),line,font=font,fill='black')
                im.save(base.with_name(base.name+f'-{n}.png'))
        except (ImportError,OSError): notes.append('png_render_failed_install_pillow_and_dejavu_font')
    if 'pdf' in formats:
        try:
            from reportlab.pdfgen.canvas import Canvas
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.ttfonts import TTFont
            from PIL import ImageFont
            fontpath=ImageFont.truetype('DejaVuSans.ttf',12).path
            pdfmetrics.registerFont(TTFont('ReceiptUnicode',fontpath))
            c=Canvas(str(base.with_suffix('.pdf'))); c.setFont('ReceiptUnicode',10); y=800
            for original in text.splitlines():
                for i in range(0,max(1,len(original)),85):
                    if y<40: c.showPage(); c.setFont('ReceiptUnicode',10); y=800
                    c.drawString(30,y,original[i:i+85]); y-=15
            c.save()
            if re.search('[\u0590-\u05ff]',text): notes.append('pdf_rtl_layout_requires_review_use_utf8_text_as_authority')
        except (ImportError,OSError): notes.append('pdf_render_failed_install_reportlab_pillow_dejavu')
    return notes

def parse_message(raw: bytes, directory: Path, *, ocr=False, render_formats=(), max_attachment_bytes=25_000_000, max_pdf_pages=30) -> tuple[dict,list[Document],list[str]]:
    directory.mkdir(parents=True,exist_ok=True)
    (directory/'original.eml').write_bytes(raw)
    msg=BytesParser(policy=policy.default).parsebytes(raw)
    meta={key:str(msg.get(key,'')) for key in ('From','To','Subject','Date','Message-ID')}
    (directory/'headers.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding='utf-8')
    documents=[]; notes=[]; body_text=[]; body_html=[]; links=[]
    for idx,part in enumerate(msg.walk()):
        if part.is_multipart(): continue
        mime=part.get_content_type(); data=part.get_payload(decode=True) or b''
        if part.defects: notes.append(f'mime_part_{idx}_defects_review')
        is_attachment=part.get_content_disposition()=='attachment' or bool(part.get_filename())
        if mime in {'text/plain','text/html'} and not is_attachment:
            charset=part.get_content_charset() or 'utf-8'
            try: value=data.decode(charset,errors='replace')
            except LookupError: value=data.decode('utf-8',errors='replace'); notes.append('unknown_charset')
            if mime=='text/html':
                body_html.append(value); parser=TextHTML(); parser.feed(value); links.extend(parser.links)
                # Keep both alternatives: they can contain different information.
                body_text.append(parser.text())
            else: body_text.append(value)
            continue
        name=Path(part.get_filename() or f'part-{idx}').name
        name=re.sub(r'[^\w.\-]','_',name)[:100] or 'attachment'
        target=directory/f'{idx:03d}-{digest(data)[:12]}-{name}'
        target.write_bytes(data)
        if len(data)>max_attachment_bytes:
            documents.append(Document(digest(data),str(target),'',mime,['attachment_over_processing_size_limit'])); continue
        if mime.startswith('text/'):
            value=data.decode(part.get_content_charset() or 'utf-8',errors='replace')
            if mime=='text/html': parser=TextHTML(); parser.feed(value); value=parser.text(); links.extend(parser.links)
            dn=[]
        else: value,dn=binary_text(target,mime,ocr,max_pdf_pages)
        documents.append(Document(digest(data),str(target),value,mime,dn))
    # Original HTML stored as .txt to prevent accidental active-content execution.
    if body_html: (directory/'inline-original-html.txt').write_text('\n'.join(body_html),encoding='utf-8')
    body='\n\n'.join(dict.fromkeys(x.strip() for x in body_text if x.strip()))
    if body:
        target=directory/'inline.txt'; target.write_text(body,encoding='utf-8')
        safe='<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'none\'"><pre dir="auto">'+html.escape(body)+'</pre>'
        (directory/'inline-safe.html').write_text(safe,encoding='utf-8')
        dn=[];capture_kind='text_fallback'
        if body_html:
            try:
                command([sys.executable,'-m','invoices_scrapper.html_capture',str(directory/'inline-original-html.txt'),str(directory/'inline-html')],timeout=60)
                capture_kind='html_render'
                dn.append('html_render_sanitized_assets_blocked_review')
            except (subprocess.SubprocessError,OSError): dn.append('html_render_unavailable_text_fallback')
        if capture_kind!='html_render':
            dn.extend(render_inline(body,directory/'inline',list(render_formats)))
            dn.append('capture_is_text_fallback_not_original_email_screenshot')
        (directory/'inline-capture.json').write_text(json.dumps({'capture_kind':capture_kind,'notes':dn},indent=2),encoding='utf-8')
        documents.insert(0,Document(digest(body.encode()),str(target),body,'text/plain',dn,capture_kind))
    if links:
        (directory/'links-review.json').write_text(json.dumps(sorted(set(links)),ensure_ascii=False,indent=2),encoding='utf-8')
        notes.append('external_links_not_fetched_review_links_file')
    if not documents: notes.append('no_documents_or_body')
    return meta,documents,notes
