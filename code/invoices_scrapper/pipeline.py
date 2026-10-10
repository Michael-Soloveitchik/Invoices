"""Resumable extraction, conservative grouping, and audit-oriented CSV exports."""
from __future__ import annotations
import csv, hashlib, json, re, sqlite3, sys, shutil
from email.parser import BytesParser
from email import policy
from email.utils import parsedate_to_datetime
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from .documents import parse_message
from .extraction import extract, ExtractionError
from .sources import SourceError

FIELDS=['id_consumption','supplier_id','vendor','category','document_id','document_number','invoice_number','related_invoice_number','date','due_date','currency','total','amount_paid','amount_due','status','kind','confidence','evidence','payment_evidence','source_id','source_url','message_id','document_path','review_flags']

def stable(*parts): return hashlib.sha256(json.dumps(parts,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()[:24]
def normalized(value): return re.sub(r'\s+',' ',value.strip().casefold())
def cell(value):
    value='' if value is None else str(value)
    # Prevent Excel/LibreOffice formulas, including leading whitespace/control chars.
    return "'"+value if value.lstrip(' \t\r\n\ufeff').startswith(('=','+','-','@')) or value.startswith(('\t','\r','\n')) else value

def csv_write(path, fields, rows):
    tmp=path.with_suffix(path.suffix+'.tmp')
    with tmp.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore'); writer.writeheader()
        for row in rows: writer.writerow({k:cell(row.get(k,'')) for k in fields})
    tmp.replace(path)

def connect(path):
    db=sqlite3.connect(path)
    db.executescript('''CREATE TABLE IF NOT EXISTS processed(source_id TEXT PRIMARY KEY, raw_sha TEXT, completed_at TEXT);
    CREATE TABLE IF NOT EXISTS records(source_id TEXT, position INTEGER, payload TEXT, PRIMARY KEY(source_id,position));
    CREATE TABLE IF NOT EXISTS issues(source_id TEXT, document_path TEXT, issue TEXT);
    CREATE TABLE IF NOT EXISTS runs(started_at TEXT, payload TEXT);''')
    return db

def annotate(record, document, index, source, meta, aliases):
    row=dict(record); vendor=normalized(row['vendor']); alias=aliases.get(vendor,{})
    supplier=alias.get('supplier_id') or 'sup_'+stable('tax' if row.get('supplier_tax_id') else 'name',row.get('supplier_tax_id') or vendor)
    # Unknown vendors never become one global supplier or merge on invoice numbers.
    if not vendor and not row.get('supplier_tax_id'): supplier='sup_unknown_'+stable(document.id,index)
    reference=row.get('related_invoice_number') or row.get('invoice_number')
    if reference and (vendor or row.get('supplier_tax_id')) and row.get('currency'):
        consumption='con_'+stable(supplier,reference.strip(),row['currency'])
    elif row.get('order_id') and row.get('currency') and vendor:
        consumption='con_'+stable(supplier,'order',row['order_id'].strip(),row['currency'])
    elif row.get('document_number') and row.get('currency') and vendor:
        consumption='con_'+stable(supplier,'document',row.get('kind','unknown'),row['document_number'].strip(),row['currency'])
    else: consumption='con_'+stable(supplier,document.id,index)
    number=row.get('document_number') or row.get('invoice_number')
    document_id='doc_'+(stable(supplier,row['kind'],number) if number else document.id)
    row.update(capture_kind=document.capture_kind,email_date=meta.get('Date',''),file_hash=document.id,extraction_method='model_from_'+document.mime,match_reason='explicit_invoice_reference' if reference else ('explicit_order_reference' if row.get('order_id') else ('explicit_document_reference' if number else 'exact_document_and_record')),id_consumption=consumption,supplier_id=supplier,category=alias.get('category') or row.get('category','unknown'),document_id=document_id,source_id=source.source_id,source_url=source.url,message_id=meta.get('Message-ID',''),document_path=document.path,review_flags=';'.join(document.notes))
    return row

def export(db, output, source_ids=None):
    rows=[json.loads(r[0]) for r in db.execute('SELECT payload FROM records ORDER BY source_id,position')]
    if source_ids is not None:rows=[r for r in rows if r['source_id'] in source_ids]
    failed_sources={r[0] for r in db.execute("SELECT DISTINCT source_id FROM issues WHERE issue LIKE 'message_failed:%'")}
    groups=defaultdict(list); candidates=defaultdict(set); cross_supplier=defaultdict(set); layout_candidates=defaultdict(set)
    for row in rows:
        groups[row['id_consumption']].append(row)
        reference=row.get('related_invoice_number') or row.get('invoice_number') or row.get('order_id') or row.get('document_number')
        if not reference and row.get('total') and row.get('currency'):
            layout_candidates[(row['supplier_id'],row.get('file_hash',row['document_id']),row['currency'],row['total'])].add(row['id_consumption'])
        if reference and row.get('vendor') and row.get('currency'):
            cross_supplier[(normalized(row['vendor']),reference,row['currency'])].add(row['id_consumption'])
        if row.get('date') and row.get('total') and row.get('currency') and row.get('vendor'):
            candidates[(row['supplier_id'],row['date'],row['currency'],row['total'])].add(row['id_consumption'])
    ambiguous=set().union(*(v for v in candidates.values() if len(v)>1)) if candidates else set()
    ambiguous.update(set().union(*(v for v in cross_supplier.values() if len(v)>1)) if cross_supplier else set())
    ambiguous.update(set().union(*(v for v in layout_candidates.values() if len(v)>1)) if layout_candidates else set())
    summary=[]
    for ident,items in groups.items():
        totals={r['total'] for r in items if r.get('total')}
        statuses={r['status'] for r in items if r['status']!='unknown'}
        flags=[]
        if any(r.get('review_flags') for r in items): flags.append('document_extraction_requires_review')
        if any(r['source_id'] in failed_sources for r in items): flags.append('stale_evidence_reprocessing_failed')
        if len({r.get('date') for r in items if r.get('date')})>1: flags.append('conflicting_document_dates_reference_reuse_review')
        if len(totals)>1: flags.append('conflicting_totals_or_payment_breakdowns')
        if len(statuses)>1: flags.append('status_history_or_conflict_review')
        if any(len({r.get(k) for r in items if r.get(k) not in (None,'')})>1 for k in ('amount_paid','amount_due','payment_date')): flags.append('payment_details_conflict_review')
        if ident in ambiguous: flags.append('possible_duplicate_distinct_references_review')
        if any(r.get('confidence',0)<0.8 for r in items): flags.append('low_confidence')
        if any(r['status'] in ('paid','partial','refund','pending','unpaid') and not r.get('payment_evidence') for r in items): flags.append('payment_claim_missing_specific_evidence')
        if not totals: flags.append('missing_total')
        if not items[0].get('currency'): flags.append('missing_currency')
        summary.append(dict(id_consumption=ident,supplier_id=items[0]['supplier_id'],vendor=items[0]['vendor'],category=items[0]['category'],currency=items[0]['currency'],total=next(iter(totals)) if len(totals)==1 else '',status=next(iter(statuses)) if len(statuses)==1 else 'unknown',evidence_count=len(items),include_in_totals='no' if flags else 'yes',review_flags=';'.join(flags)))
        for row in items:
            row['review_flags']=';'.join(filter(None,[row.get('review_flags',''),*flags]))
    # User-facing relational views: one expense, many evidence rows; one statement may link many expenses.
    expenses=[]
    for summary_row in summary:
        items=groups[summary_row['id_consumption']]
        def agreed(key):
            values={str(r[key]) for r in items if r.get(key) not in (None,'')}
            return next(iter(values)) if len(values)==1 else ''
        expenses.append(dict(id_consumption=summary_row['id_consumption'],supplier_id=summary_row['supplier_id'],supplier_name=summary_row['vendor'],category=summary_row['category'],description=agreed('description'),expense_date=agreed('date'),period_start=agreed('period_start'),period_end=agreed('period_end'),currency=summary_row['currency'],amount_total=summary_row['total'],amount_vat=agreed('amount_vat'),amount_paid=agreed('amount_paid'),amount_due=agreed('amount_due'),payment_status=summary_row['status'],payment_date=agreed('payment_date'),due_date=agreed('due_date'),transaction_type='refund' if agreed('kind')=='refund' or summary_row['status']=='refund' else ('credit' if agreed('kind')=='credit' else 'charge'),needs_review='yes' if summary_row['review_flags'] else 'no',review_reason=summary_row['review_flags'],countable=summary_row['include_in_totals']))
    docs=[dict(document_id=r['document_id'],id_consumption=r['id_consumption'],supplier_id=r['supplier_id'],document_type=r['kind'],document_number=r.get('document_number') or r.get('invoice_number',''),order_id=r.get('order_id',''),document_date=r['date'],email_id=r['source_id'],email_date=r.get('email_date',''),source_url=r['source_url'],file_path=r['document_path'],file_hash=r.get('file_hash',''),extraction_method=r.get('extraction_method',''),match_reason=r.get('match_reason',''),evidence=r['evidence'],payment_evidence=r.get('payment_evidence',''),confidence=r['confidence'],capture_kind=r.get('capture_kind',''),rendered_files=';'.join(str(p) for p in sorted(Path(r['document_path']).parent.glob('inline-*.png'))) if Path(r['document_path']).name=='inline.txt' else '') for r in rows]
    csv_write(output/'expenses.csv',['id_consumption','supplier_id','supplier_name','category','description','expense_date','period_start','period_end','currency','amount_total','amount_vat','amount_paid','amount_due','payment_status','payment_date','due_date','transaction_type','needs_review','review_reason','countable'],expenses)
    csv_write(output/'documents.csv',['document_id','id_consumption','supplier_id','document_type','document_number','order_id','document_date','email_id','email_date','source_url','file_path','file_hash','extraction_method','match_reason','evidence','payment_evidence','confidence','capture_kind','rendered_files'],docs)
    csv_write(output/'evidence.csv',FIELDS,rows)
    csv_write(output/'consumptions.csv',['id_consumption','supplier_id','vendor','category','currency','total','status','evidence_count','include_in_totals','review_flags'],summary)
    issues=[dict(zip(['source_id','document_path','issue'],row)) for row in db.execute('SELECT source_id,document_path,issue FROM issues')]
    if source_ids is not None:issues=[r for r in issues if r['source_id'] in source_ids or r['source_id']=='SOURCE']
    issues.extend({'source_id':r['source_id'],'document_path':r['document_path'],'issue':r['review_flags']} for r in rows if r['review_flags'])
    csv_write(output/'review.csv',['source_id','document_path','issue'],issues)
    return len(rows),len(summary),len(issues)

def run(source, output: Path, config: dict, *, allow_external=False, reprocess=False):
    output=Path(output).resolve(); output.mkdir(parents=True,exist_ok=True)
    reports=output/'reports';reports.mkdir(exist_ok=True)
    today=datetime.now(timezone.utc)
    for offset in range(25):
        year,month=divmod(today.year*12+today.month-1-offset,12)
        (output/f'{year:04d}'/f'{month+1:02d}').mkdir(parents=True,exist_ok=True)
    db=connect(reports/'state.sqlite3'); started=datetime.now(timezone.utc).isoformat()
    counts=dict(seen=0,processed=0,skipped=0,failed=0,documents=0)
    source_error=None;selected_ids=set()
    try:
        for message in source.messages():
            counts['seen']+=1;selected_ids.add(message.source_id)
            sha=hashlib.sha256(message.raw).hexdigest()
            previous=db.execute('SELECT raw_sha FROM processed WHERE source_id=?',(message.source_id,)).fetchone()
            if not reprocess and previous and previous[0]==sha:
                counts['skipped']+=1; continue
            try:
                email_date=parsedate_to_datetime(str(BytesParser(policy=policy.default).parsebytes(message.raw,headersonly=True).get('Date','')))
                archive_month=email_date.strftime('%Y/%m')
            except (ValueError,TypeError,OverflowError): archive_month='unknown/unknown'
            directory=output/archive_month/stable(message.source_id)/sha[:24]
            try:
                meta,documents,notes=parse_message(message.raw,directory,ocr=bool(config.get('ocr',False)),render_formats=sorted(set(config.get('render_formats',[]))|{'png'}),max_attachment_bytes=config.get('max_attachment_bytes',25_000_000),max_pdf_pages=config.get('max_pdf_pages',30))
                counts['documents']+=len(documents); rows=[]; issues=[('',n) for n in notes]
                (directory/'source.json').write_text(json.dumps({'source_id':message.source_id,'url':message.url,'raw_sha256':sha},ensure_ascii=False,indent=2),encoding='utf-8')
                for doc in documents:
                    if Path(doc.path).name=='inline.txt' and not list(Path(doc.path).parent.glob('inline-*.png')):
                        raise ExtractionError('Mandatory inline PNG capture missing; install Pillow/render dependencies and fonts, then retry')
                    issues.extend((doc.path,n) for n in doc.notes)
                    if not doc.text.strip(): continue
                    # Never silently truncate. Overlong input is a visible failure; increase limit or split manually.
                    extracted=extract(doc.text,config.get('llm',{'provider':'mock'}),allow_external=allow_external)
                    if not extracted: issues.append((doc.path,'no_financial_records_detected_review_if_expected'))
                    dates={r.get('date') for r in extracted if r.get('date')}
                    if len(dates)==1:
                        document_month=next(iter(dates))[:7].replace('-','/')
                        archive=output/document_month/stable(message.source_id)/sha[:24]
                        if archive!=directory:
                            archive.mkdir(parents=True,exist_ok=True)
                            original=Path(doc.path)
                            files=[original]
                            if original.name=='inline.txt': files=list(directory.glob('inline*'))
                            for file in files: shutil.copy2(file,archive/file.name)
                            shutil.copy2(directory/'source.json',archive/'source.json')
                            doc.path=str(archive/original.name)
                    seen_records={}
                    for index,record in enumerate(extracted):
                        fingerprint=json.dumps(record,sort_keys=True,ensure_ascii=False)
                        canonical_index=seen_records.setdefault(fingerprint,index)
                        if canonical_index!=index: issues.append((doc.path,'repeated_identical_extraction_same_document'))
                        row=annotate(record,doc,canonical_index,message,meta,config.get('supplier_aliases',{}))
                        shared_reference=record.get('related_invoice_number') or record.get('invoice_number') or record.get('order_id') or record.get('document_number')
                        peers=[r for r in extracted if (r.get('related_invoice_number') or r.get('invoice_number') or r.get('order_id') or r.get('document_number'))==shared_reference]
                        if shared_reference and len({json.dumps(r,sort_keys=True,ensure_ascii=False) for r in peers})>1:
                            row['id_consumption']='con_'+stable(row['supplier_id'],doc.id,canonical_index)
                            row['match_reason']='multiple_records_shared_document_reference_manual_review'
                            row['review_flags']=';'.join(filter(None,[row['review_flags'],'shared_document_reference_multiple_records_review']))
                        rows.append(row)
                with db:
                    db.execute('DELETE FROM records WHERE source_id=?',(message.source_id,)); db.execute('DELETE FROM issues WHERE source_id=?',(message.source_id,))
                    db.executemany('INSERT INTO records VALUES(?,?,?)',[(message.source_id,i,json.dumps(r,ensure_ascii=False)) for i,r in enumerate(rows)])
                    db.executemany('INSERT INTO issues VALUES(?,?,?)',[(message.source_id,p,n) for p,n in issues])
                    db.execute('INSERT OR REPLACE INTO processed VALUES(?,?,?)',(message.source_id,sha,datetime.now(timezone.utc).isoformat()))
                counts['processed']+=1
            except Exception as exc:
                counts['failed']+=1
                # Avoid storing exception strings: provider/network errors may contain private snippets or tokens.
                with db:
                    db.execute('DELETE FROM processed WHERE source_id=?',(message.source_id,))
                    db.execute("DELETE FROM issues WHERE source_id=? AND issue LIKE 'message_failed:%'",(message.source_id,))
                    db.execute('INSERT INTO issues VALUES(?,?,?)',(message.source_id,str(directory),'message_failed:'+type(exc).__name__))
                detail=str(exc) if isinstance(exc,ExtractionError) else type(exc).__name__
                print(f"Message failed ({detail}); preserved originals if parsed. Retry after correcting config/tools.",file=sys.stderr)
            if counts['seen']%25==0: print(json.dumps(counts),file=sys.stderr)
    except Exception as exc:
        source_error=str(exc) if isinstance(exc,SourceError) else type(exc).__name__; counts['failed']+=1
        print('Source scan incomplete: '+source_error,file=sys.stderr)
        with db: db.execute('INSERT INTO issues VALUES(?,?,?)',('SOURCE','','source_scan_incomplete:'+source_error))
    if source_error is None:
        with db: db.execute("DELETE FROM issues WHERE source_id='SOURCE'")
    scope_issues=getattr(source,'issues',[])
    with db:
        for issue in scope_issues:db.execute('INSERT INTO issues VALUES(?,?,?)',('SOURCE',issue[1],issue[2]+':'+issue[0]))
    counts['failed']+=len(scope_issues)
    n,c,i=export(db,reports,selected_ids if hasattr(source,'scope') else None); counts.update(evidence_rows=n,consumptions=c,review_items=i,scan_complete=source_error is None and not getattr(source,'truncated',False) and not scope_issues)
    counts.update(scope=getattr(source,'scope',{'mode':'full_scan'}),filtered_out=getattr(source,'filtered_out',0),sample_truncated=getattr(source,'truncated',False))
    with db: db.execute('INSERT INTO runs VALUES(?,?)',(started,json.dumps(counts)))
    (reports/'last-run.json').write_text(json.dumps({'started_at':started,**counts},indent=2),encoding='utf-8')
    db.close(); return counts
