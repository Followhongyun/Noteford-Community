"""Mac login-time and incremental media completion, independent of the visible chat."""
from __future__ import annotations
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from .automatic_media import AutomaticMedia
from .cdn_download import DownloadError
from .downloaded_media import _atomic_write, retained_download
from .mac_attachments import entries, message_xml, present, complete, cached_file
from .mac_cdn import download_media, download_emoji, download_tp, live_root
from .mac_media import media_error
from .media import MediaArtifact


def _cached(config, uid, kind='image'):
    if 'project_root' not in config or 'account' not in config:
        return None
    result=retained_download(Path(config['project_root'])/'data/media-cache',config['account'],uid,kind)
    return MediaArtifact('ready',kind,str(result[0]),None,result[1],False).to_dict()if result else None


def enrich(core, item):
    kind=item['type']['kind']
    if kind in {'emoji','gif'}:
        item['media']=({'kind':'image','md5':None,'status':'disabled'}
                       if core.config.get('parse_stickers',True)is False else
                       _cached(core.config,item['uid'])or {'kind':'image','md5':None,'status':'missing'})
    elif kind in {'merged_messages','message_history'}:
        item['record_items']=present(core.config,item)
    elif kind=='file' and 'project_root' in core.config:
        if path:=cached_file(core.config,item['uid']):
            item['media']=MediaArtifact('ready','file',str(path),None,path.stat().st_size,False).to_dict()
    elif kind=='image'and (item.get('media')or {}).get('status')!='ready':
        preview=_cached(core.config,item['uid']+':preview')
        if preview:
            preview.update(status='ready_thumbnail',download_error='local_original_unavailable')
            item['media']=preview


def recheck(core, uid, *, should_continue=None):
    found=core.repository.message_by_uid(uid)
    if found is None:raise ValueError('message_not_found')
    item=found['message'];kind=item['type']['kind'];error=None;artifact=None
    if kind in {'emoji','gif'} and core.config.get('parse_stickers',True)is False:
        enrich(core,item)
        return {'conversation':found['conversation'],'selection':{'state':'following'},'messages':[item]}
    root=message_xml(core.config,uid)
    try:
        if kind in {'image','video'}:
            resolver=core._media_resolver();resolver.force_copy=True;resolver.original_allowed=None
            args=(item['timestamp'],(item.get('media')or {}).get('md5'),uid,core.media_cache_root)
            def local():
                return resolver.export_image(item['source']['table'],*args)if kind=='image'else resolver.export_video(*args)
            artifact=local()
            if artifact.status!='ready':
                try:
                    node=root.find('img')if root is not None and kind=='image'else None
                    if node is not None and node.get('tpurl'):
                        try:
                            _,original=download_tp(core.config,uid,node.attrib,should_continue=should_continue)
                            if not original:error='local_original_unavailable'
                        except DownloadError as exc:
                            if str(exc) in {'download_cancelled','auto_size_limit','insufficient_space'}:raise
                            if not any(node.get(key) for key in ('cdnbigimgurl','cdnmidimgurl')):raise
                            download_media(core.config,uid,kind,should_continue=should_continue)
                    else:download_media(core.config,uid,kind,should_continue=should_continue)
                    artifact=local()
                except DownloadError as exc:
                    # WeChat may have finished writing the original while CDN was tried.
                    artifact=local()
                    if artifact.status!='ready':error=str(exc)
            item['media']=artifact.to_dict()
            enrich(core,item)
        elif kind in {'emoji','gif'}:
            node=root.find('emoji')if root is not None else None
            if node is None:raise DownloadError('missing_download_reference')
            download_emoji(core.config,uid,node.attrib,should_continue=should_continue)
            enrich(core,item)
        elif kind in {'merged_messages','message_history'}:
            paths=[]
            for entry in entries(root,uid):
                try:
                    if path:=complete(core.config,entry,should_continue=should_continue):paths.append(str(path))
                except DownloadError as exc:
                    error=error or str(exc)
                    if str(exc)=='download_cancelled':break
            enrich(core,item)
            if not item.get('record_items'):raise DownloadError('missing_download_reference')
            marker=core.media_cache_root/'record-completion'/(hashlib.sha256((core.account+uid).encode()).hexdigest()+'.json')
            if not error:_atomic_write(marker,json.dumps(paths).encode())
            item['media']={'status':'ready'if not error else 'missing','kind':'record','md5':None,
                           'path':str(marker)if not error else None}
        elif kind=='file':
            resolver=core._media_resolver()
            artifact=resolver.export_file(item['timestamp'],item.get('title'),uid,core.media_cache_root)
            item['media']=artifact.to_dict()
            if artifact.status!='ready':
                app=root.find('appmsg')if root is not None else None
                if app is None:raise DownloadError('missing_download_reference')
                attach=app.find('appattach')
                fields={x.tag:(x.text or '').strip()for x in attach}if attach is not None else {}
                fields.update(cdndataurl=fields.get('cdnattachurl',''),cdndatakey=fields.get('aeskey',''),
                              fullmd5=fields.get('md5')or app.findtext('md5',''),datasize=fields.get('totallen','0'),
                              datafmt=fields.get('fileext','bin'),filetype='5')
                path=complete(core.config,{'kind':'file','uid':uid,'fields':fields},should_continue=should_continue)
                item['media']=MediaArtifact('ready','file',str(path),None,path.stat().st_size,False).to_dict()
        else:raise ValueError('unsupported_media_kind')
    except DownloadError as exc:
        error=str(exc)
    if not item.get('media'):item['media']={'status':'missing','kind':kind,'md5':None}
    if error:item['media']['download_error']=error
    if should_continue is None:core.automatic_media.record_manual_result(uid,item['media'])
    return {'conversation':found['conversation'],'selection':{'state':'following'},'messages':[item]}


class MacAutomaticMedia(AutomaticMedia):
    def __init__(self,core):
        super().__init__(core)
        self.path=core.project_root/'data/mac_automatic_media.sqlite'
        self._credential_stamp=None
        # Once per upgrade, retry objects previously rejected solely because
        # Tencent returned a re-encoded rendition without the original hash.
        con=self._state()
        try:
            if con.execute('PRAGMA user_version').fetchone()[0]<2:
                with con:
                    con.execute("UPDATE jobs SET status='media_unavailable',attempts=0,retry_at=0 WHERE status='integrity_failed'")
                    con.execute('PRAGMA user_version=2')
            if con.execute('PRAGMA user_version').fetchone()[0]<3:
                with con:
                    # New local sticker decoding and original-upgrade retry path.
                    con.execute("UPDATE jobs SET status='media_unavailable',attempts=0,retry_at=0 WHERE status IN ('missing_download_reference','local_original_unavailable')")
                    con.execute('PRAGMA user_version=3')
        finally:con.close()

    def _record(self, row, status, attempts, retry_at, path=None):
        if status == 'local_original_unavailable':
            # Keep a usable preview while periodically checking for an original.
            # Add an hour to the normal backoff and avoid permanent three-try
            # suspension. The persisted deadline survives app restarts.
            retry_at += 3600
            attempts = min(attempts, 2)
        super()._record(row, status, attempts, retry_at, path)

    def _rows(self,uid=None):
        if self.core.config.get('auto_media_loading',True)is False:return
        try:live_root(self.core.config)
        except DownloadError:return
        with closing(sqlite3.connect(Path(self.core.config['index_path']).resolve().as_uri()+'?mode=ro',uri=True))as db:
            db.row_factory=sqlite3.Row
            query='''SELECT m.uid,m.row_hash,m.create_time,c.username,m.local_type
                FROM messages m JOIN conversations c ON c.account=m.account AND c.chat_table=m.chat_table
                LEFT JOIN message_media_policy p ON p.uid=m.uid
                WHERE m.account=? AND COALESCE(p.automatic,1)=1 AND
                (m.local_type IN(3,43,47) OR (m.local_type & 65535)=49)'''
            params=[self.core.account]
            if self.core.config.get('parse_stickers',True)is False:
                query+=' AND m.local_type!=47'
            if uid:query+=' AND m.uid=?';params.append(uid)
            for row in db.execute(query+' ORDER BY m.create_time DESC,m.uid',params):
                if row['local_type']not in {3,43,47}:
                    found=self.core.repository.message_by_uid(row['uid'])
                    if not found or found['message']['type']['kind']not in {'merged_messages','message_history','file'}:continue
                yield row

    def _candidate(self,now):
        # Transient errors remain retryable across app restarts and renewed login
        # credentials. Terminal per-object errors stay paused until revision changes.
        con=self._state()
        try:
            with con:
                try:
                    cache=live_root(self.core.config).parent.parent/'app_data/net/cdncomm/cdninfo_new.cache'
                    stat=cache.stat(); fingerprint=(stat.st_size,stat.st_mtime_ns)
                except (OSError, DownloadError):
                    fingerprint=None
                if fingerprint is not None and fingerprint!=self._credential_stamp:
                    self._credential_stamp=fingerprint
                    con.execute("DELETE FROM cooldown WHERE account=? AND reason IN ('session_unavailable','account_mismatch')",(self.core.account,))
                    con.execute("UPDATE jobs SET status='session_unavailable',attempts=0,retry_at=0 WHERE account=? AND status IN ('session_unavailable','account_mismatch')",(self.core.account,))
                con.execute("UPDATE jobs SET status='media_unavailable',attempts=0 WHERE account=? AND retry_at<=? AND status IN ('session_unavailable','network_unavailable','download_timeout','storage_unavailable','insufficient_space','media_unavailable','cdn_rejected','cdn_not_found','integrity_failed','invalid_response')",(self.core.account,now))
                for row in con.execute("SELECT uid,path FROM jobs WHERE account=? AND status='ready'",(self.core.account,)):
                    path=Path(row['path']or '')
                    if path.parent.name=='record-completion':
                        try:valid=all(Path(p).is_file()for p in json.loads(path.read_text()))
                        except(OSError,ValueError,TypeError):valid=False
                        if not valid:con.execute("UPDATE jobs SET status='media_unavailable',attempts=0,retry_at=0 WHERE account=? AND uid=?",(self.core.account,row['uid']))
        finally:con.close()
        return super()._candidate(now)
