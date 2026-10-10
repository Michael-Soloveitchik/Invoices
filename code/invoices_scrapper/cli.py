import argparse,json,os,sys
from pathlib import Path
from .pipeline import run
from .scope import ScopedSource,epoch
from .sources import EmlSource,GmailSource,ImapSource,SourceError

def main(argv=None):
    argv=list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0]=='auth':
        from .auth import main as auth_main
        return auth_main(argv[1:])
    parser=argparse.ArgumentParser(description='Read-only email expense extraction. Full scan; never follows email links.',epilog='Explicit local OAuth setup: invoices-scrapper auth --help (ordinary scans never open consent).')
    parser.add_argument('--source',choices=['eml','gmail','imap'],default='eml')
    parser.add_argument('--input',type=Path,default=Path('examples/mail'))
    default_output=Path.cwd().parent if Path.cwd().name=='code' and Path.cwd().parent.name=='Invoices' else Path.cwd()/'Invoices'
    parser.add_argument('--output',type=Path,default=default_output,help='Invoices archive root; CSV/state are under reports and evidence under YEAR/MONTH')
    parser.add_argument('--config',type=Path,default=Path('config.example.json'))
    parser.add_argument('--allow-external-llm',action='store_true',help='Authorize sending ALL scanned message bodies and extracted attachment text, including non-financial mail, to the configured LLM endpoint')
    parser.add_argument('--start',help='Inclusive ISO timestamp with timezone, e.g. 2026-08-10T00:00:00+03:00')
    parser.add_argument('--end',help='Exclusive ISO timestamp with timezone')
    parser.add_argument('--max-messages',type=int,help='Explicit sample cap; incomplete coverage reported')
    parser.add_argument('--reprocess',action='store_true',help='Re-extract processed messages after changing model/prompts/settings')
    args=parser.parse_args(argv)
    try:
        config=json.loads(args.config.read_text(encoding='utf-8'))
        if args.source!='eml' and config.get('llm',{}).get('provider','mock')=='mock':
            parser.error('Mock extraction is for local synthetic .eml fixtures only. Choose a real provider for Gmail/IMAP.')
        if config.get('llm',{}).get('provider','mock')!='mock' and not args.allow_external_llm:
            parser.error('Real LLM providers require --allow-external-llm. ALL scanned bodies/attachments, including non-financial mail, may be transmitted and contain sensitive personal data.')
        scope=config.get('scope',{})
        start=args.start or scope.get('start');end=args.end or scope.get('end');maximum=args.max_messages or scope.get('max_messages')
        source={'eml':lambda:EmlSource(args.input),'gmail':lambda:GmailSource(start_epoch=epoch(start),end_epoch=epoch(end),token_file=config.get('gmail',{}).get('token_file')),'imap':ImapSource}[args.source]()
        if start or end or maximum is not None:source=ScopedSource(source,start,end,maximum,gmail=args.source=='gmail')
        result=run(source,args.output,config,allow_external=args.allow_external_llm,reprocess=args.reprocess)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 1 if result['failed'] or not result['scan_complete'] else 0
    except SourceError as exc:
        print(f'Source configuration error: {exc}'); return 2
    except (OSError,ValueError) as exc:
        print(f'Configuration/source error: {type(exc).__name__}. Check configuration and environment.'); return 2
