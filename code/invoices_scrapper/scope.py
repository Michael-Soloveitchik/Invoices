"""Explicit timestamp windows and sample limits, before any LLM transmission."""
from datetime import datetime
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime


def epoch(value):
    if not value:return None
    parsed=datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.microsecond:raise ValueError('Scope timestamps require timezone and whole seconds')
    return parsed.timestamp()

class ScopedSource:
    def __init__(self,source,start=None,end=None,max_messages=None,*,gmail=False):
        self.source=source;self.start=epoch(start);self.end=epoch(end)
        if self.start is not None and self.end is not None and self.start>=self.end:raise ValueError('start must precede end')
        if max_messages is not None and (type(max_messages)is not int or max_messages<1):raise ValueError('max_messages must be positive')
        self.max_messages=max_messages;self.gmail=gmail;self.truncated=False;self.issues=[];self.filtered_out=0
        self.scope={'start_inclusive':start,'end_exclusive':end,'max_messages':max_messages,'date_basis':'gmail_internal_received_date' if gmail else 'email_Date_header','external_text_scope':'matching_messages_only'}
    def messages(self):
        emitted=0
        for msg in self.source.messages():
            if self.start is not None or self.end is not None:
                try:
                    if self.gmail:
                        stamp=getattr(msg,'received_at',None)
                        if stamp is None:raise ValueError('missing internal received timestamp')
                    else:
                        parsed=parsedate_to_datetime(str(BytesParser(policy=policy.default).parsebytes(msg.raw,headersonly=True).get('Date','')))
                        if parsed.tzinfo is None:raise ValueError('missing timezone')
                        stamp=parsed.timestamp()
                except (TypeError,ValueError,OverflowError):
                    self.issues.append((msg.source_id,'','date_scope_unverifiable_not_sent_to_llm'));continue
                if (self.start is not None and stamp<self.start) or (self.end is not None and stamp>=self.end):
                    self.filtered_out+=1;continue
            if self.max_messages is not None and emitted>=self.max_messages:
                self.truncated=True;return
            emitted+=1;yield msg
