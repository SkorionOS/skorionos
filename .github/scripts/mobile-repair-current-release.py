#!/usr/bin/env python3
"""One-release delta audit/repair draft. No service startup, delete, or mkdir.
Requires localhost-only Alist v3.45.0; safe login/mount from environment,
or an already mounted service with ALIST_TOKEN supplied.
Official API source: server/handles/{fsread,fsmanage,fsup}.go at v3.45.0.
Only missing/size-mismatched/SHA256-mismatched exact release assets are repaired.
All writes remain beneath fixed DELTA; full images/history are never written.
"""
import argparse, hashlib, http.client, json, os, re, shutil, sys, tempfile, time
from pathlib import Path
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

TAG = '56-7_d462926'
REPO = 'SkorionOS/skorionos'
DELTA = '/139Yun/Public/img/' + TAG + '/delta'
BASE = 'http://127.0.0.1:5244'
TOKEN = os.environ.get('ALIST_TOKEN', '')
CHUNK = 4 * 1024 * 1024
EVENTS=[]
REPORT={}
REPORT_PATH=Path('/tmp/mobile-repair-report.json')

class SafeError(Exception): pass

def emit(**kw):
    EVENTS.append(kw)
    print(json.dumps(kw, ensure_ascii=False), flush=True)

def api(endpoint, body):
    # Do not print raw responses/errors: Alist may echo signed URLs or credentials.
    req = Request(BASE + '/api/' + endpoint, data=json.dumps(body).encode(),
                  headers={'Authorization': TOKEN, 'Content-Type':'application/json'})
    try:
        with urlopen(req, timeout=180) as r: data=json.load(r)
    except Exception as e:
        raise SafeError('Alist '+endpoint+' transport error ('+type(e).__name__+')') from None
    if data.get('code') != 200:
        raise SafeError('Alist '+endpoint+' returned code '+str(data.get('code')))
    return data.get('data')

def bootstrap():
    global TOKEN
    if TOKEN:return  # Caller supplied an already mounted local service.
    password=os.environ.get('ALIST_ADMIN_PASSWORD','')
    auth=os.environ.get('MOBILE_CLOUD_AUTHORIZATION','')
    if not password or not auth:raise SafeError('Alist bootstrap environment missing')
    d=api('auth/login',{'username':'admin','password':password})
    TOKEN=d.get('token','')
    if not TOKEN:raise SafeError('Alist login returned no token')
    print('::add-mask::'+TOKEN,flush=True)
    addition={'authorization':auth,'root_folder_id':'/','type':'personal_new',
              'cloud_id':'','custom_upload_part_size':0,'report_real_size':True,
              'use_large_thumbnail':False}
    api('admin/storage/create',{'mount_path':'/139Yun','driver':'139Yun','order':0,
        'remark':'Temporary scoped release delta audit', 'addition':json.dumps(addition)})


def remote_list():
    out={}; page=1
    while True:
        d=api('fs/list', {'path':DELTA,'password':'','refresh':True,'page':page,'per_page':1000})
        rows=d.get('content') or []
        for o in rows:
            if o['name'] in out: raise SafeError('Duplicate remote name')
            out[o['name']]=o
        if len(out)>=int(d['total']): return out
        if not rows: raise SafeError('Incomplete remote listing')
        page+=1

def hashes(obj):
    # Only log validated hash strings, never unknown API fields.
    result={}
    value=obj.get('hash_info') or {}
    if isinstance(value,dict):
        for k,v in value.items():
            k=str(k).lower().replace('-','')
            if k in ('sha256','sha1','md5') and isinstance(v,str) and re.fullmatch('[0-9a-fA-F]+',v):
                result[k]=v.lower()
    return result

def release_assets():
    req=Request(f'https://api.github.com/repos/{REPO}/releases/tags/{TAG}',
                headers={'User-Agent':'skos-targeted-delta-repair','Accept':'application/vnd.github+json'})
    try:
        with urlopen(req, timeout=90) as r: release=json.load(r)
    except Exception as e: raise SafeError('GitHub metadata transport error ('+type(e).__name__+')') from None
    if release['tag_name']!=TAG: raise SafeError('Wrong release')
    out={}
    for a in release['assets']:
        n=a['name']
        if not (n.startswith('skorionos-'+TAG+'-') and n.endswith('.skdelta')): continue
        if any(x in n for x in ('hyprland','cosmic','cinnamon')): continue
        if '/' in n or '\\' in n or not re.fullmatch(r'[A-Za-z0-9_.-]+',n): raise SafeError('Invalid asset name')
        if a.get('state')!='uploaded' or not re.fullmatch('sha256:[a-f0-9]{64}',a.get('digest','')):
            raise SafeError('Missing trustworthy GitHub asset digest')
        if n in out: raise SafeError('Duplicate source name')
        out[n]=a
    if not out: raise SafeError('No expected deltas')
    return out

def status(a, o):
    if o is None: return 'missing'
    if o.get('is_dir'): raise SafeError('Expected filename is a directory')
    if int(o['size'])!=int(a['size']): return 'bad_size'
    h=hashes(o).get('sha256')
    if h and h!=a['digest'][7:]: return 'bad_sha256'
    return 'hash_ok' if h else 'size_ok_hash_unavailable'

def checked_path(n):
    if '/' in n or '\\' in n or n in ('.','..'): raise SafeError('Unsafe name')
    return DELTA+'/'+n

def read_stream(url, dest=None):
    # Cloud URLs come only from local authenticated fs/get; no Alist/GitHub token is sent.
    u=urlsplit(url)
    if u.scheme not in ('http','https'): raise SafeError('Invalid download scheme')
    if u.scheme=='http' and u.hostname not in ('127.0.0.1','localhost'):
        raise SafeError('Refusing unencrypted external download')
    h=hashlib.sha256();size=0
    try:
        with urlopen(Request(url,headers={'User-Agent':'skos-targeted-delta-repair'}),timeout=300) as r:
            while data:=r.read(CHUNK):
                h.update(data);size+=len(data)
                if dest:dest.write(data)
    except Exception as e: raise SafeError('Download transport error ('+type(e).__name__+')') from None
    return size,h.hexdigest()

def readback(a):
    obj=api('fs/get', {'path':checked_path(a['name']),'password':''})
    if int(obj['size'])!=int(a['size']): raise SafeError('Readback metadata size mismatch')
    size,h=read_stream(obj['raw_url'])
    if size!=a['size'] or 'sha256:'+h!=a['digest']: raise SafeError('Readback SHA256/size mismatch')
    emit(event='readback_verified',name=a['name'],size=size,sha256=h)

def rename_backup(o):
    n=o['name'];stamp=time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())
    backup=n+'.repair-backup-'+stamp
    before=remote_list()
    if backup in before: raise SafeError('Backup collision')
    current=before.get(n)
    if current is None or str(current.get('id'))!=str(o.get('id')) or current['size']!=o['size']:
        raise SafeError('Remote changed before rename')
    error=None
    try:api('fs/rename', {'path':checked_path(n),'name':backup,'overwrite':False})
    except SafeError as e:error=e
    # A transport error is uncertain, not permission to repeat the write.
    after=remote_list();saved=after.get(backup)
    if n in after or saved is None or saved['size']!=o['size'] or str(saved.get('id'))!=str(o.get('id')):
        raise error or SafeError('Backup rename not confirmed; stop')
    emit(event='backup_preserved',name=n,backup=backup,size=saved['size'])
    return backup

def put(a,path):
    # Official FsStream checks Overwrite:false before entering 139 driver.
    if a['name'] in remote_list(): raise SafeError('Upload destination unexpectedly exists')
    conn=http.client.HTTPConnection('127.0.0.1',5244,timeout=7200)
    try:
        conn.putrequest('PUT','/api/fs/put')
        for k,v in {'Authorization':TOKEN,'File-Path':quote(checked_path(a['name']),safe=''),
                    'Content-Length':str(a['size']),'Content-Type':'application/octet-stream',
                    'Overwrite':'false','As-Task':'false','X-File-Sha256':a['digest'][7:]}.items():
            conn.putheader(k,v)
        conn.endheaders()
        with path.open('rb') as f:
            while data:=f.read(CHUNK):conn.send(data)
        response=conn.getresponse();data=json.loads(response.read(1024*1024))
        if response.status!=200 or data.get('code')!=200:raise SafeError('Upload response unsuccessful')
    except Exception as e:
        # Never retry uploads blindly. Caller verifies remote state and exits if uncertain.
        emit(event='upload_response_uncertain',name=a['name'],error_type=type(e).__name__)
    finally:conn.close()
    for _ in range(12):
        o=remote_list().get(a['name'])
        if o and o.get('size')==a['size']:
            readback(a);return
        time.sleep(5)
    raise SafeError('Upload not confirmed; backup retained, do not retry blindly')

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('mode',nargs='?',choices=['audit','repair'],default=os.environ.get('REPAIR_MODE','audit'))
    parser.add_argument('--max-files',type=int,default=1)
    parser.add_argument('--only-name',default='skorionos-56-7_d462926-gnome-full.from_56-3_9d3fe89.skdelta',help='Exact asset allowlist for repair; audit remains complete')
    parser.add_argument('--report',default='/tmp/mobile-repair-report.json')
    args=parser.parse_args()
    global REPORT_PATH, REPORT
    REPORT_PATH=Path(args.report)
    if args.mode not in ('audit','repair'):raise SafeError('Invalid mode')
    bootstrap()
    if not 1<=args.max_files<=100:raise SafeError('Invalid max-files')
    expected=release_assets();remote=remote_list();rows=[]
    for n,a in sorted(expected.items()):
        o=remote.get(n);row={'name':n,'expected_size':a['size'],'expected_sha256':a['digest'][7:],
            'actual_size':o.get('size') if o else None,'hashes':hashes(o or {}),'status':status(a,o)}
        rows.append(row);emit(**row)
    report={'tag':TAG,'path':DELTA,'files':rows,'expected_count':len(rows),
            'extra_names':sorted(set(remote)-set(expected))}
    REPORT=report
    REPORT_PATH.write_text(json.dumps(report,indent=2)+'\n')
    todo=[expected[r['name']] for r in rows if r['status'] in ('missing','bad_size','bad_sha256')]
    total_needs_repair=len(todo)
    if args.only_name:
        if args.only_name not in expected:raise SafeError('only-name is not a filtered release asset')
        todo=[a for a in todo if a['name']==args.only_name]
    emit(event='audit_summary',expected=len(rows),needs_repair=total_needs_repair,repair_queue=len(todo),mode=args.mode)
    if args.mode=='audit':return
    # No full-image, unrelated, or historical paths can enter this queue.
    for a in todo[:args.max_files]:
        now=remote_list().get(a['name']);s=status(a,now)
        if s not in ('missing','bad_size','bad_sha256'):continue
        if shutil.disk_usage('/tmp').free < 3*a['size']+1024**3:raise SafeError('Insufficient safe disk headroom')
        with tempfile.TemporaryDirectory(prefix='skos-one-delta-') as temp:
            path=Path(temp)/'payload'
            with path.open('wb') as f:size,h=read_stream(a['browser_download_url'],f)
            if size!=a['size'] or 'sha256:'+h!=a['digest']:raise SafeError('GitHub source SHA256/size mismatch')
            emit(event='source_verified',name=a['name'],size=size,sha256=h)
            # Preserve corrupt cloud content, never delete it.
            if now is not None:rename_backup(now)
            put(a,path)
    final=remote_list();remaining=[n for n,a in expected.items() if status(a,final.get(n)) in ('missing','bad_size','bad_sha256')]
    REPORT['remaining_names']=remaining
    REPORT['final_files']=[{'name':n,'status':status(a,final.get(n)),
        'actual_size':final[n]['size'] if n in final else None,
        'hashes':hashes(final.get(n) or {})} for n,a in sorted(expected.items())]
    REPORT['readback_verified']=[e for e in EVENTS if e.get('event')=='readback_verified']
    emit(event='final_summary',expected=len(expected),remaining=len(remaining),remaining_names=remaining)
    if remaining:sys.exit(2)

if __name__=='__main__':
    try:main()
    except SafeError as e:
        emit(event='stopped',reason=str(e));sys.exit(1)
    except Exception as e:
        emit(event='stopped',reason='Unexpected '+type(e).__name__+'; no raw exception logged');sys.exit(1)

    finally:
        REPORT['events']=EVENTS
        REPORT_PATH.write_text(json.dumps(REPORT,indent=2)+'\n')
