"""Explicit user-run Google OAuth bootstrap; never invoked by ordinary scans.

Only the local user runs authorize() to grant persistent gmail.readonly access.
No connected-app credentials are read or exported. Token refresh only uses an
already granted, explicitly configured local authorized-user token file.
"""
from __future__ import annotations
import argparse,json,os,stat,tempfile
from pathlib import Path

SCOPES=('https://www.googleapis.com/auth/gmail.readonly',)
TOKEN_URI='https://oauth2.googleapis.com/token'
AUTH_URIS={'https://accounts.google.com/o/oauth2/auth','https://accounts.google.com/o/oauth2/v2/auth'}

class AuthError(ValueError):pass

def _libraries():
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        return InstalledAppFlow,Credentials,Request
    except ImportError:raise AuthError('Install the optional auth extra: pip install -e ".[auth]"') from None

def _check_path(path:Path):
    for component in (path.absolute(),*path.absolute().parents):
        try:
            if stat.S_ISLNK(component.lstat().st_mode):raise AuthError('Credential paths must not contain symbolic links')
        except FileNotFoundError:continue
        except OSError:raise AuthError('Cannot inspect credential path safely') from None
    if os.name!='posix':raise AuthError('Local token storage requires POSIX owner-only permissions; use an environment access token on other systems')

def _directory(path:Path):
    _check_path(path)
    try:
        path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if path.parent.stat().st_uid!=os.getuid():raise AuthError('Credentials directory must be owned by the current user')
        os.chmod(path.parent,0o700)
    except OSError:raise AuthError('Cannot create or protect credentials directory') from None

def _read_json(path:Path,*,private=True):
    _check_path(path)
    try:
        fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
        with os.fdopen(fd,'r',encoding='utf-8') as stream:
            info=os.fstat(stream.fileno())
            if info.st_uid!=os.getuid():raise AuthError('Credential file must be owned by the current user')
            if not stat.S_ISREG(info.st_mode) or info.st_size>65536:raise AuthError('Credential file must be a small regular JSON file')
            if private and info.st_mode & 0o077:raise AuthError('Credential file permissions must be owner-only (chmod 600)')
            data=json.load(stream)
        if not isinstance(data,dict):raise AuthError('Credential file must contain a JSON object')
        return data
    except (OSError,ValueError,UnicodeError) as exc:
        if isinstance(exc,AuthError):raise
        raise AuthError('Cannot read credential JSON; check file and permissions') from None

def _scope_check(data):
    scopes=data.get('scopes',[])
    if isinstance(scopes,str):scopes=scopes.split()
    if not isinstance(scopes,(list,tuple)) or not all(isinstance(s,str) for s in scopes) or set(scopes)!=set(SCOPES):raise AuthError('Only the exact gmail.readonly scope is accepted; use the explicit auth command to create a dedicated token')

def _validate_token(data):
    if data.get('type','authorized_user')!='authorized_user':raise AuthError('Only authorized-user credentials are supported')
    if data.get('token_uri',TOKEN_URI)!=TOKEN_URI:raise AuthError('Token endpoint must be the official Google endpoint')
    _scope_check(data)
    for key in ('client_id','client_secret','refresh_token'):
        if not isinstance(data.get(key),str) or not data[key]:raise AuthError('Authorized-user token is missing required fields')

def _save(path:Path,credentials):
    _directory(path)
    if path.exists():_check_path(path)
    try:
        serialized=credentials.to_json();data=json.loads(serialized);_validate_token(data)
        if len(serialized)>65536:raise AuthError('Token data exceeds size limit')
        fd,name=tempfile.mkstemp(prefix='.token-',suffix='.tmp',dir=path.parent)
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as stream:
                os.fchmod(stream.fileno(),0o600);stream.write(serialized);stream.flush();os.fsync(stream.fileno())
            os.replace(name,path);os.chmod(path,0o600)
        finally:
            if os.path.exists(name):os.unlink(name)
    except AuthError:raise
    except Exception:raise AuthError('Unable to save local token securely') from None

def authorize(client_file:Path,token_file:Path,replace=False):
    """Explicit local-user action only. Opens a loopback Google consent flow."""
    client_file=Path(client_file);token_file=Path(token_file)
    _directory(client_file);_directory(token_file)
    if client_file.absolute()==token_file.absolute():raise AuthError('Client and token files must be different')
    if token_file.exists() and (not token_file.is_file() or token_file.stat().st_uid!=os.getuid()):raise AuthError('Existing token target must be a regular file owned by the current user')
    if client_file.exists() and (not client_file.is_file() or client_file.stat().st_uid!=os.getuid()):raise AuthError('Client JSON must be a regular file owned by the current user')
    if token_file.exists() and not replace:raise AuthError('Token already exists; use --replace-token only when intentionally reauthorizing')
    try:os.chmod(client_file,0o600)
    except OSError:raise AuthError('Place your downloaded desktop client JSON in the credentials directory first') from None
    data=_read_json(client_file)
    installed=data.get('installed')
    if not isinstance(installed,dict) or 'web' in data:raise AuthError('Use a Google OAuth Desktop app client JSON')
    if not isinstance(installed.get('auth_uri'),str) or installed['auth_uri'] not in AUTH_URIS or installed.get('token_uri')!=TOKEN_URI:raise AuthError('Client JSON must use official Google authorization and token endpoints')
    if not isinstance(installed.get('client_id'),str) or not installed['client_id'].endswith('.apps.googleusercontent.com') or not isinstance(installed.get('client_secret'),str) or not installed['client_secret']:raise AuthError('Desktop client JSON is incomplete')
    Flow,_,_=_libraries()
    try:
        flow=Flow.from_client_config(data,scopes=list(SCOPES))
        credentials=flow.run_local_server(host='127.0.0.1',port=0,open_browser=True,authorization_prompt_message='Open the Google consent page in your browser.',success_message='Authorization complete. You can close this window.',access_type='offline',prompt='consent',include_granted_scopes='false',timeout_seconds=300)
        granted=getattr(credentials,'granted_scopes',None)
        if granted is not None and set(granted)!=set(SCOPES):raise AuthError('Google returned unexpected scopes; token was not saved')
        _save(token_file,credentials)
    except AuthError:raise
    except Exception:raise AuthError('Local Google authorization failed or was canceled; no token was saved') from None

class TokenProvider:
    """Refresh existing authorized-user credentials; never starts consent."""
    def __init__(self,token_file:Path):
        self.path=Path(token_file)
        _check_path(self.path)
        try:
            if self.path.parent.stat().st_uid!=os.getuid():raise AuthError('Credentials directory must be owned by the current user')
            if self.path.parent.stat().st_mode & 0o077:raise AuthError('Credentials directory permissions must be owner-only (chmod 700)')
        except OSError:raise AuthError('Cannot read the local credentials directory') from None
        data=_read_json(self.path);_validate_token(data)
        _,Credentials,self.Request=_libraries()
        try:self.credentials=Credentials.from_authorized_user_info(data,scopes=list(SCOPES))
        except Exception:raise AuthError('Cannot load the local authorized-user token') from None
    def get(self,force_refresh=False):
        try:
            if force_refresh or not self.credentials.valid:
                if not self.credentials.refresh_token:raise AuthError('Token cannot refresh; run the explicit auth command locally')
                self.credentials.refresh(self.Request())
                granted=getattr(self.credentials,'granted_scopes',None)
                if granted is not None and set(granted)!=set(SCOPES):raise AuthError('Refreshed token has unexpected scopes')
                _save(self.path,self.credentials)
            token=self.credentials.token
            if not isinstance(token,str) or not token or any(c.isspace() for c in token):raise AuthError('Google token is invalid')
            return token
        except AuthError:raise
        except Exception:raise AuthError('Google token refresh failed; check connection or reauthorize locally') from None

def main(argv=None):
    parser=argparse.ArgumentParser(description='Explicit LOCAL USER OAuth consent for Gmail read-only; never exports connected-app credentials')
    default=Path.cwd().parent/'credentials' if Path.cwd().name=='code' and Path.cwd().parent.name=='Invoices' else Path.cwd()/'Invoices'/'credentials'
    parser.add_argument('--credentials-dir',type=Path,default=default)
    parser.add_argument('--replace-token',action='store_true',help='Intentionally replace an existing local token after new consent')
    args=parser.parse_args(argv)
    try:
        authorize(args.credentials_dir/'client_secret.json',args.credentials_dir/'token.json',replace=args.replace_token)
        print('Local Gmail read-only token saved with owner-only permissions. Do not upload or commit credentials.');return 0
    except AuthError as exc:print(str(exc));return 2
