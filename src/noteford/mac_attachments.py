"""Structured Mac merged records, with private references kept out of UI/API payloads."""
from __future__ import annotations
import hashlib
import json
import re
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from .cdn_download import DownloadError, CHUNK, _request
from .decode import decode_text_blob
from .downloaded_media import _atomic_write, retained_download
from .mac_decode import _xml, _text
from .mac_cdn import live_root, session, retain_file, _allowed


def message_xml(config, uid):
    with closing(sqlite3.connect(Path(config['index_path']).resolve().as_uri()+'?mode=ro',uri=True))as db:
        row=db.execute('SELECT message_content,compress_content FROM messages WHERE account=? AND uid=?',
                       (config['account'],uid)).fetchone()
    if row:
        for blob in reversed(row):
            text,error=decode_text_blob(blob)
            if not error and '<'in text:
                root=_xml(text[text.find('<'):])
                if root is not None:return root
    return None


def entries(root, parent_uid, *, depth=0, budget=None):
    budget=budget if budget is not None else [300]
    if root is None or depth>=5:return []
    app=root if root.tag=='appmsg' else root.find('appmsg')
    record=(_xml(_text(app,'recorditem')) if app is not None else root)
    if record is None:record=app.find('recordinfo')if app is not None else None
    if record is None:return []
    output=[]
    kinds={'1':'text','2':'image','3':'voice','4':'video','5':'link','6':'location',
           '8':'file','16':'contact_card','17':'merged_messages','19':'mini_program'}
    for index,node in enumerate(record.findall('./datalist/dataitem')):
        if budget[0]<=0:break
        budget[0]-=1
        fields={child.tag:(child.text or '').strip()for child in node}
        uid=parent_uid+':record:'+str(index)
        kind=kinds.get(node.get('datatype'),'unknown')
        output.append({'uid':uid,'kind':kind,'fields':fields,'node':node})
        if kind=='merged_messages':
            nested=node.find('recordinfo')
            if nested is None:nested=_xml(fields.get('recordxml')or fields.get('recorditem',''))
            output.extend(entries(nested,uid,depth=depth+1,budget=budget))
    return output


def _file_cache(config, uid):
    digest=hashlib.sha256((config['account']+'\0'+uid).encode()).hexdigest()
    return Path(config['project_root'])/'data/media-cache/record-files'/digest


def cached_file(config, uid):
    folder=_file_cache(config,uid)
    try:
        if (folder/'manifest.json').stat().st_size>4096:return None
        m=json.loads((folder/'manifest.json').read_text())
        if m['account']!=config['account']or m['uid']!=uid or not re.fullmatch('[a-f0-9]{64}',m['sha256']):return None
        ext=m['extension']
        if not re.fullmatch('[a-z0-9]{1,12}',ext):return None
        path=folder/(m['sha256']+'.'+ext)
        if not path.resolve().is_relative_to(folder.resolve())or path.stat().st_size!=m['bytes']:return None
        with path.open('rb')as f:
            if hashlib.file_digest(f,'sha256').hexdigest()!=m['sha256']:return None
        return path
    except (OSError,ValueError,KeyError,TypeError):return None


def cache_file(config,uid,data,extension):
    digest=hashlib.sha256(data).hexdigest()
    extension=extension.lower()
    if not re.fullmatch('[a-z0-9]{1,12}',extension):extension='bin'
    folder=_file_cache(config,uid);path=folder/(digest+'.'+extension)
    _atomic_write(path,data)
    _atomic_write(folder/'manifest.json',json.dumps({'account':config['account'],'uid':uid,
        'sha256':digest,'bytes':len(data),'extension':extension}).encode())
    return path


def cache_file_path(config, uid, source, extension, should_continue=None):
    from .downloaded_media import promote_download
    extension = extension.lower()
    if not re.fullmatch('[a-z0-9]{1,12}', extension):extension='bin'
    folder = _file_cache(config,uid)
    path, digest, size = promote_download(source,folder,extension,should_continue)
    _atomic_write(folder/'manifest.json',json.dumps({'account':config['account'],'uid':uid,
        'sha256':digest,'bytes':size,'extension':extension}).encode())
    return path


def transfer(config, fields, work, should_continue=None):
    reference,key=fields.get('cdndataurl',''),fields.get('cdndatakey','')
    digest=fields.get('fullmd5','').lower()
    if not(re.fullmatch('[a-fA-F0-9]{32,4096}',reference)and re.fullmatch('[a-fA-F0-9]{32}',key)
           and re.fullmatch('[a-f0-9]{32}',digest)):
        raise DownloadError('missing_download_reference')
    try:size=int(fields['datasize']);filetype=int(fields.get('filetype','5'))
    except(ValueError,KeyError):raise DownloadError('missing_download_reference')from None
    from .download_policy import check_size
    check_size(size)
    if size<=0:raise DownloadError('invalid_response')
    from .download_policy import ensure_space
    ensure_space(work, size)
    if filetype not in {1,2,3,4,5,6,8,9,19}:raise DownloadError('unsupported_media_kind')
    req=session(config,key)
    req.update(fileid=reference.encode(),filetype=str(filetype).encode(),downpicformat=b'0',support_format=b'0')
    dec=Cipher(algorithms.AES(bytes.fromhex(key)),modes.ECB()).decryptor()
    output=work/'attachment';start=0;total=None;tail=b''
    with output.open('wb')as f:
        while total is None or start<total:
            _allowed(should_continue)
            ensure_space(work, size-start)
            end=65535 if total is None else min(total-1,start+CHUNK-1)
            req.update(offset=str(start).encode(),rangestart=str(start).encode(),rangeend=str(end).encode())
            result=_request(req,time.monotonic()+30)
            try:
                count=int(result['totalsize']);begin=int(result['rangestart']);data=result['filedata']
            except(KeyError,ValueError):raise DownloadError('invalid_response')from None
            if count!=((size//16)+1)*16 or begin!=start or not data or len(data)%16 or len(data)>end-start+1 or start+len(data)>count:
                raise DownloadError('invalid_response')
            if total is not None and total!=count:raise DownloadError('invalid_response')
            total=count;plain=tail+dec.update(data);f.write(plain[:-16]);tail=plain[-16:];start+=len(data)
            from .download_progress import report
            report(start,total)
        tail+=dec.finalize();pad=tail[-1]
        if not 1<=pad<=16 or tail[-pad:]!=bytes([pad])*pad:raise DownloadError('integrity_failed')
        f.write(tail[:-pad])
    with output.open('rb')as f:
        actual = hashlib.file_digest(f,'md5').hexdigest()
    variant = actual != digest
    cdn_digest = result.get('rawfilemd5',b'').decode('ascii',errors='replace').lower()
    if output.stat().st_size != size or (cdn_digest and actual != cdn_digest) or (variant and
            (filetype not in {1,2,3,4} or actual != cdn_digest or
             result.get('substituteftype') not in {str(filetype).encode(),b'9'})):
        raise DownloadError('integrity_failed')
    return output, 'cdn_variant' if variant else 'message_original'


def complete(config, item, *, should_continue=None):
    live_root(config)
    kind,uid,fields=item['kind'],item['uid'],item['fields']
    if kind in {'image','video'}:
        cached=retained_download(Path(config['project_root'])/'data/media-cache',config['account'],uid,kind)
        if cached:return cached[0]
    elif cached:=cached_file(config,uid):return cached
    if kind not in {'image','video','file','voice'}:return None
    root=Path(config['project_root'])/'data/media-cache';root.mkdir(parents=True,exist_ok=True)
    from .download_policy import ensure_space
    ensure_space(root)
    with tempfile.TemporaryDirectory(prefix='.record-',dir=root)as folder:
        work=Path(folder);path,verification=transfer(config,fields,work,should_continue)
        _allowed(should_continue)
        if kind in {'image','video'}:
            return retain_file(config,uid,kind,path,verification,work,should_continue)
        if kind=='voice':
            from .mac_media import MacVoiceDecoder
            converted=work/'voice.wav';result=MacVoiceDecoder().decode(path,converted)
            if not result.ok:raise DownloadError('unsupported_download_format')
            return cache_file_path(config,uid,converted,'wav',should_continue)
        return cache_file_path(config,uid,path,fields.get('datafmt','bin'),should_continue)


def present(config, parent):
    result=[]
    for entry in entries(message_xml(config,parent['uid']),parent['uid']):
        fields,kind,uid=entry['fields'],entry['kind'],entry['uid']
        media=None
        if kind in {'image','video','voice','file'}:
            cached=retained_download(Path(config['project_root'])/'data/media-cache',config['account'],uid,kind)if kind in {'image','video'}else None
            path=cached[0]if cached else cached_file(config,uid)
            media={'status':'ready'if path else 'missing','kind':kind,'md5':None,'path':str(path)if path else None,
                   'bytes':path.stat().st_size if path else None,'encrypted':False}
        url=fields.get('weburl','')
        try:timestamp=int(fields.get('srcMsgCreateTime')or parent['timestamp'])
        except ValueError:timestamp=parent['timestamp']
        result.append({'uid':uid,'type':{'kind':kind,'msg_type':0,'sub_type':0},'timestamp':timestamp,
            'time':fields.get('sourcetime'),'sender':{'name':fields.get('sourcename'),'username':None,'from_me':False},
            'title':fields.get('datatitle')or None,'text':fields.get('datadesc',''),
            'url':url if url.startswith(('https://','http://'))else None,'card':None,'quote':None,'status':None,'media':media})
    if len(result)>=300:
        result.append({'uid':parent['uid']+':record:limit','type':{'kind':'text','msg_type':1,'sub_type':0},
            'timestamp':parent['timestamp'],'time':None,'sender':{'name':'Noteford Community','username':None,'from_me':False},
            'title':None,'text':'这份记录较长，当前最多展开 300 项。','url':None,'card':None,'quote':None,'status':None,'media':None})
    return result
