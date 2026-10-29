import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from noteford.automatic_media import AutomaticMedia
from noteford.indexer import ensure_index_schema
from noteford.metadata import ensure_metadata_schema
from noteford.media_ranges import MediaRanges
from noteford import cdn_download as cdn


def setup(tmp_path):
    index = tmp_path/'index.db'
    ensure_index_schema(index)
    with sqlite3.connect(index) as con:
        ensure_metadata_schema(con)
        for user,state in [('follow','following'),('paused','paused'),('other','unfollowed')]:
            con.execute('INSERT INTO conversations(account,chat_table,username,display_name,is_group,session_type,last_timestamp,sort_timestamp) VALUES(?,?,?,?,0,0,0,0)',('acct','Msg_'+user,user,user))
            if state == 'unfollowed': continue
            con.execute('INSERT INTO conversation_selection(account,username,chat_table,state,source_dbs,selected_at,updated_at) VALUES(?,?,?,?,?,?,?)',('acct',user,'Msg_'+user,state,'[]','now','now'))
        start = int(datetime(2026,10,1).timestamp())
        rows=[('image',3,start,'follow'),('video',43,start+1,'follow'),('early',3,start-1,'follow'),
              ('late',43,start+86400,'follow'),('paused',3,start,'paused'),('other',43,start,'other'),('voice',34,start,'follow')]
        for i,(uid,kind,stamp,user) in enumerate(rows):
            con.execute('INSERT INTO messages(uid,account,source_db,chat_table,local_id,server_id,local_type,create_time,row_hash,indexed_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (uid,'acct','message_0.db','Msg_'+user,i,str(i),kind,stamp,'v1','now'))
        con.execute('UPDATE message_media_policy SET range_exempt=0')
    def ranges():
        return MediaRanges(tmp_path,'2026-10-01T00:00:00',today=date(2026,10,2))
    ranges().save('follow','2026-10-01','2026-10-01')
    core=SimpleNamespace(project_root=tmp_path, account='acct',config={'index_path':str(index)},_media_ranges=ranges)
    calls=[]
    def complete(uid, *, should_continue):
        assert should_continue()
        calls.append(uid)
        path=tmp_path/(uid+'.media');path.write_bytes(b'complete')
        return {'messages':[{'media':{'status':'ready','path':str(path)}}]}
    core.recheck_media=complete
    return core,calls,index


def test_auto_scope_resume_and_missing_cache(tmp_path):
    core,calls,index=setup(tmp_path);stop=threading.Event();worker=AutomaticMedia(core)
    assert worker.run_one(stop,now=100)
    assert worker.run_one(stop,now=100)
    assert not worker.run_one(stop,now=100)
    assert calls==['image','video']
    # New worker represents a restarted service. Completed files are not downloaded again.
    restarted=AutomaticMedia(core)
    assert not restarted.run_one(stop,now=1000)
    (tmp_path/'image.media').unlink()
    assert restarted.run_one(stop,now=1000)
    assert calls==['image','video','image']
    assert worker.summary('follow')['completed']==2
    # Enlarging the range makes the new date eligible without editing the queue.
    core._media_ranges().save('follow','2026-10-01','2026-10-02')
    assert worker.run_one(stop,now=1000)
    assert calls[-1]=='late'


def test_failure_backoff_does_not_block_other_files_and_survives_restart(tmp_path):
    core,calls,index=setup(tmp_path);good=core.recheck_media
    def complete(uid,**kwargs):
        if uid=='image':
            calls.append(uid)
            return {'messages':[{'media':{'status':'ready_thumbnail','download_error':'download_timeout'}}]}
        return good(uid,**kwargs)
    core.recheck_media=complete;worker=AutomaticMedia(core);stop=threading.Event()
    assert worker.run_one(stop,now=100)
    assert worker.run_one(stop,now=101)
    assert not AutomaticMedia(core).run_one(stop,now=102)
    assert calls==['image','video']
    assert worker.run_one(stop,now=221)
    assert calls[-1]=='image'


def test_account_cooldown_and_stop(tmp_path):
    core,calls,index=setup(tmp_path)
    def complete(uid,**kwargs):
        calls.append(uid)
        return {'messages':[{'media':{'status':'missing','download_error':'account_mismatch'}}]}
    core.recheck_media=complete;worker=AutomaticMedia(core);stop=threading.Event()
    assert worker.run_one(stop,now=100)
    assert not AutomaticMedia(core).run_one(stop,now=101)
    assert calls==['image']
    stop.set()
    assert not worker.run_one(stop,now=10000)


def test_pause_or_range_change_rechecked_inside_download(tmp_path):
    core,calls,index=setup(tmp_path);worker=AutomaticMedia(core);stop=threading.Event()
    def complete(uid, *, should_continue):
        assert should_continue()
        with sqlite3.connect(index) as con:
            con.execute("UPDATE conversation_selection SET state='paused' WHERE username='follow'")
        assert not should_continue()
        return {'messages':[{'media':{'status':'missing','download_error':'download_cancelled'}}]}
    core.recheck_media=complete
    assert worker.run_one(stop,now=100)
    assert not worker.run_one(stop,now=200)
    with sqlite3.connect(index) as con:
        con.execute("UPDATE conversation_selection SET state='following' WHERE username='follow'")
    assert worker.eligible('image','v1')
    core._media_ranges().save('follow','2026-10-02','2026-10-02')
    assert not worker.eligible('image','v1')


def test_cancelled_download_never_requests_network(tmp_path,monkeypatch):
    monkeypatch.setattr(cdn,'_request',lambda *a:pytest.fail('network must not be called'))
    with pytest.raises(cdn.DownloadError,match='download_cancelled'):
        cdn.download_media({},'uid','image',should_continue=lambda:False)


def test_scheduler_is_separate_and_stoppable():
    from noteford.service import MultiLibraryApi
    api=MultiLibraryApi.__new__(MultiLibraryApi)
    api._automatic_thread=None;api._automatic_stop=threading.Event();api._automatic_wake=threading.Event()
    ran=threading.Event()
    record = SimpleNamespace(id='library')
    api.registry=SimpleNamespace(list=lambda:[record], current=lambda:record)
    def run(stop):ran.set();return False
    api._core=lambda _:SimpleNamespace(automatic_media=SimpleNamespace(run_one=run))
    api.start_automatic_media()
    assert ran.wait(2)
    original=api._automatic_thread;api.start_automatic_media();assert api._automatic_thread is original
    api.stop_automatic_media();assert not original.is_alive()


def test_cancel_after_first_chunk_stops_remaining_transfer(tmp_path, monkeypatch):
    import hashlib
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    data=b'a'*90000;pad=16-len(data)%16
    cipher=Cipher(algorithms.AES(b'k'*16),modes.ECB()).encryptor()
    encrypted=cipher.update(data+bytes([pad])*pad)+cipher.finalize()
    requests=[]
    def request(fields, deadline):
        start=int(fields['rangestart']);end=int(fields['rangeend']);requests.append(start)
        return {'retcode':b'0','rangestart':str(start).encode(),'totalsize':str(len(encrypted)).encode(),
                'filedata':encrypted[start:end+1],'rawfilemd5':hashlib.md5(data).hexdigest().encode(),
                'substituteftype':b'1'}
    monkeypatch.setattr(cdn,'_request',request)
    with pytest.raises(cdn.DownloadError,match='download_cancelled'):
        cdn._transfer({'aeskey':(b'k'*16).hex()},'a'*64,'image',{},tmp_path,999999999,
                      should_continue=lambda:not requests)
    assert requests==[0]
    assert not (tmp_path/'manifest.json').exists()

def test_old_session_cooldown_recovers_within_five_minutes(tmp_path):
    core,calls,index=setup(tmp_path);worker=AutomaticMedia(core)
    con=worker._state()
    with con:
        con.execute('INSERT INTO cooldown VALUES(?,?,?)',('acct',100+21600,'session_unavailable'))
    con.close()
    assert not worker.run_one(threading.Event(),now=100)
    assert not worker.run_one(threading.Event(),now=399)
    assert worker.run_one(threading.Event(),now=401)
    assert calls==['image']


def test_new_messages_synced_while_disabled_are_never_automatically_loaded(tmp_path):
    from noteford.media_loading import set_enabled, mark_manual
    core,calls,index=setup(tmp_path);worker=AutomaticMedia(core);stop=threading.Event()
    set_enabled(index,False)
    with sqlite3.connect(index) as con:
        con.execute("INSERT INTO messages(uid,account,source_db,chat_table,local_id,server_id,local_type,create_time,row_hash,indexed_at) SELECT 'new-off',account,source_db,chat_table,99,'99',43,create_time,row_hash,indexed_at FROM messages WHERE uid='video'")
    assert worker.run_one(stop,now=100)
    assert worker.run_one(stop,now=100)
    assert not worker.run_one(stop,now=100)
    set_enabled(index,True)
    assert not worker.run_one(stop,now=1000)
    mark_manual(index,'new-off')
    assert not worker.run_one(stop,now=1001)
    assert calls==['image','video']

@pytest.mark.parametrize('status', ['cdn_not_found', 'cdn_expired', 'cdn_rejected', 'missing_download_reference', 'unsupported_download_format'])
def test_permanent_failure_waits_for_message_revision(tmp_path, status):
    core,calls,index=setup(tmp_path)
    def fail(uid,**kwargs):
        calls.append(uid)
        return {'messages':[{'media':{'status':'missing','download_error':status}}]}
    core.recheck_media=fail
    worker=AutomaticMedia(core)
    stop=threading.Event()
    assert worker.run_one(stop,now=100)
    assert worker.run_one(stop,now=101)
    assert not AutomaticMedia(core).run_one(stop,now=1000000)
    details=worker.summary('follow')['failures']
    assert len(details)==2 and all(x['paused'] for x in details)
    with sqlite3.connect(index) as con:
        con.execute("UPDATE messages SET row_hash='v2' WHERE uid='image'")
    assert worker.run_one(stop,now=1000001)
    assert calls==['image','video','image']


def test_transient_failure_stops_after_three_attempts(tmp_path):
    core,calls,index=setup(tmp_path)
    def fail(uid,**kwargs):
        calls.append(uid)
        return {'messages':[{'media':{'status':'missing','download_error':'download_timeout'}}]}
    core.recheck_media=fail
    worker=AutomaticMedia(core)
    for now in (100,1000,10000):
        assert worker.run_one(threading.Event(),now=now)
        assert worker.run_one(threading.Event(),now=now)
    assert not AutomaticMedia(core).run_one(threading.Event(),now=1000000)
    assert len(calls)==6


def test_manual_success_clears_old_failure_without_enrolling_new_media(tmp_path):
    core,calls,index=setup(tmp_path);worker=AutomaticMedia(core)
    row=dict(next(worker._rows('image')))
    worker._record(row,'cdn_not_found',9,999999)
    p=tmp_path/'loaded';p.write_bytes(b'complete')
    worker.record_manual_result('image',{'status':'ready','path':str(p)})
    worker.record_manual_result('video',{'status':'ready','path':str(p)})
    summary=worker.summary('follow')
    assert summary['completed']==1 and summary['waiting']==0


def test_expiry_survives_restart_but_not_message_revision_or_account_change(tmp_path):
    core, calls, index = setup(tmp_path)
    worker = AutomaticMedia(core)
    worker._record(dict(next(worker._rows('image'))), 'cdn_expired', 1, 999999)
    assert AutomaticMedia(core).expired_ids(['image', 'video']) == {'image'}
    core.account = 'different-account'
    assert worker.expired_ids(['image']) == set()
    core.account = 'acct'
    with sqlite3.connect(index) as con:
        con.execute("UPDATE messages SET row_hash='v2' WHERE uid='image'")
    assert worker.expired_ids(['image']) == set()


@pytest.mark.parametrize('local_type', [3, (6 << 32) | 49])
def test_manual_only_expiry_is_remembered_without_enabling_automatic_loading(tmp_path, local_type):
    core, calls, index = setup(tmp_path)
    with sqlite3.connect(index) as con:
        con.execute("UPDATE messages SET local_type=? WHERE uid='image'", (local_type,))
        con.execute("UPDATE message_media_policy SET automatic=0 WHERE uid='image'")
    worker = AutomaticMedia(core)
    assert not list(worker._rows('image'))
    worker.record_manual_result('image', {'status':'ready_thumbnail','download_error':'cdn_expired'})
    restarted = AutomaticMedia(core)
    assert restarted.expired_ids(['image']) == {'image'}
    assert not list(restarted._rows('image'))
    assert restarted.summary('follow')['failures'][0]['paused']
    worker.record_manual_result('video', {'status':'thumbnail_only','download_error':'cdn_rejected'})
    assert restarted.expired_ids(['video']) == set()
    core.account = 'acct'
    with sqlite3.connect(index) as con:
        con.execute("UPDATE messages SET row_hash='v2' WHERE uid='image'")
    assert worker.expired_ids(['image']) == set()


def test_new_sync_bypasses_retired_range_without_enrolling_history(tmp_path):
    from noteford.media_loading import ensure_policy, set_enabled
    core, calls, index = setup(tmp_path)
    worker = AutomaticMedia(core)
    assert {row['uid'] for row in worker._rows()} == {'image', 'video'}
    def add(uid):
        with sqlite3.connect(index) as con:
            con.execute("INSERT INTO messages(uid,account,source_db,chat_table,local_id,server_id,local_type,create_time,row_hash,indexed_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (uid,'acct','message_0.db','Msg_follow',100 if uid == 'new-on' else 101,uid,3,int(datetime(2026,10,3).timestamp()),'v1','now'))
    add('new-on')
    set_enabled(index, False); add('new-off')
    set_enabled(index, True)
    with sqlite3.connect(index) as con:
        ensure_policy(con)
    assert {row['uid'] for row in worker._rows()} == {'image', 'video', 'new-on'}
    assert worker.eligible('new-on', 'v1')
    assert not worker.eligible('new-off', 'v1')
    assert not worker.eligible('early', 'v1')
    assert not worker.eligible('late', 'v1')



def test_size_policy_skips_without_retries_and_resumes_when_increased(tmp_path):
    from noteford.download_policy import check_size
    core,calls,index=setup(tmp_path)
    core.config['auto_media_max_mb']=20
    worker=AutomaticMedia(core);stop=threading.Event()
    complete=core.recheck_media
    def sized(uid, **kwargs):
        check_size(25*1024*1024)
        return complete(uid,**kwargs)
    core.recheck_media=sized
    assert worker.run_one(stop,now=100)
    assert worker.run_one(stop,now=100)
    assert not worker.run_one(stop,now=1000)
    assert not calls
    # Manual work is outside the automatic context and remains permitted.
    assert sized('image',should_continue=lambda:True)['messages'][0]['media']['status']=='ready'
    core.config['auto_media_max_mb']=30
    assert worker.run_one(stop,now=1001)
    assert worker.run_one(stop,now=1001)
    assert worker.summary('follow')['completed']==2
