import json
import threading
import time
from types import SimpleNamespace
import pytest
from noteford.indexer import _connect_index
from noteford.metadata import ensure_metadata_schema
from noteford.repository import WeChatRepository
from noteford.service import LocalApi, MultiLibraryApi
from noteford.sync_state import SyncState, read_public_sync_state


def test_same_second_pages_do_not_drop_or_repeat_messages(tmp_path):
    path = tmp_path / 'index.db'
    con = _connect_index(path)
    ensure_metadata_schema(con)
    with con:
        con.execute("INSERT INTO conversations(account,chat_table,username,display_name,is_group,session_type,last_timestamp,sort_timestamp) VALUES('acct','Msg_a','a','A',0,0,100,99999)")
        con.execute("INSERT INTO conversations(account,chat_table,username,display_name,is_group,session_type,last_timestamp,sort_timestamp) VALUES('acct','Msg_b','b','B',0,0,200,0)")
        for i in range(351):
            con.execute("INSERT INTO messages(uid,account,source_db,chat_table,local_id,server_id,local_type,sort_seq,create_time,message_content,row_hash,indexed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (str(i), 'acct','db','Msg_a',i,str(i),1,i,100,b'synthetic','hash','now'))
        con.execute("INSERT INTO messages(uid,account,source_db,chat_table,local_type,sort_seq,server_id,create_time,message_content,row_hash,indexed_at) VALUES('b','acct','db','Msg_b',1,1,'b',200,'newer','h','now')")
    con.close()
    repo = WeChatRepository(path,'acct')
    page = repo.list_messages('a',limit=100)
    seen = []
    while page['messages']:
        seen.extend(m['uid'] for m in page['messages'])
        cursor = page['next_before_uid']
        if cursor is None: break
        page = repo.list_messages('a',limit=100,before_uid=cursor)
    assert len(seen) == len(set(seen)) == 351
    assert repo.list_conversations()[0]['username'] == 'b'
    with pytest.raises(ValueError): repo.list_messages('b',before_uid='1')


def test_sync_failure_busy_and_restart_receipts(tmp_path, monkeypatch):
    import noteford.service as module
    api = LocalApi({'project_root':str(tmp_path),'index_path':str(tmp_path/'index.db'), 'wechat_root':str(tmp_path/'wechat'),'account':'synthetic','candidates_path':str(tmp_path/'keys.json')})
    api._automatic_wake = threading.Event()
    api.sync_state.update(state='ready', last_success_at='previous-success')
    api.refresh_lock.acquire()
    assert api.refresh() == {'ok':False,'error':'refresh_busy'}
    assert not api._automatic_wake.is_set()
    assert api.sync_state.read()['state'] == 'ready'
    api.refresh_lock.release()
    def unavailable(**kwargs): raise FileNotFoundError('private account and path must not leak')
    monkeypatch.setattr(module,'_refresh_library',unavailable)
    with pytest.raises(FileNotFoundError): api.refresh()
    state = api.sync_state.read()
    assert state['state'] == 'failed'
    assert state['last_success_at'] == 'previous-success'
    assert 'private' not in api.sync_state.path.read_text()
    api.sync_state.update(state='running')
    assert SyncState(tmp_path).read()['state'] == 'interrupted'
    assert not api.refresh_lock.locked()
    assert api._automatic_wake.is_set()


def test_partial_selected_refresh_does_not_advance_full_success(tmp_path, monkeypatch):
    import noteford.service as module
    api = LocalApi({'project_root':str(tmp_path),'index_path':str(tmp_path/'index.db'), 'wechat_root':str(tmp_path/'wechat'),'account':'synthetic','candidates_path':str(tmp_path/'keys.json')})
    api.sync_state.update(state='ready', last_success_at='previous-success')
    monkeypatch.setattr(module, '_refresh_library', lambda **kwargs: {
        'selected_refresh': [
            {'ok': True, 'username': 'safe'},
            {'ok': False, 'username': 'private-user', 'error': 'PrivatePathError'},
        ],
        'archive_status': {'last_success_at': 'archive-success'},
    })
    result = api.refresh(trigger='interval')
    state = api.sync_state.read()
    assert result['ok'] is False
    assert result['archive_ok'] is True
    assert result['failed_conversations'] == 1
    assert state['state'] == 'partial'
    assert state['error'] == 'selected_refresh_partial'
    assert state['last_success_at'] == 'previous-success'
    assert state['failed_conversations'] == 1
    assert 'private-user' not in api.sync_state.path.read_text(encoding='utf-8')
    assert 'PrivatePathError' not in api.sync_state.path.read_text(encoding='utf-8')


def test_permission_failure_is_actionable_and_keeps_last_success(tmp_path, monkeypatch):
    import noteford.service as module
    from noteford.mac_live_sync import SourcePermissionRequired
    api = LocalApi({'project_root':str(tmp_path),'index_path':str(tmp_path/'index.db'), 'wechat_root':str(tmp_path/'wechat'),'account':'synthetic','candidates_path':str(tmp_path/'keys.json')})
    api.sync_state.update(state='ready', last_success_at='2026-10-19T00:00:00+00:00')
    def denied(**kwargs):
        raise SourcePermissionRequired('private source path')
    monkeypatch.setattr(module, '_refresh_library', denied)
    with pytest.raises(SourcePermissionRequired):
        api.refresh()
    state = read_public_sync_state(api.sync_state.path)
    assert state['state'] == 'failed'
    assert state['error'] == 'source_permission_required'
    assert state['recent_receipts'][-1]['error'] == 'source_permission_required'
    assert state['last_success_at'] == '2026-10-19T00:00:00+00:00'
    assert 'private' not in api.sync_state.path.read_text()
    assert not api.refresh_lock.locked()


def test_source_change_reason_survives_persistence_and_public_status(tmp_path):
    state = SyncState(tmp_path)
    state.record_receipt(
        trigger='source_change', started_at='2026-10-21T00:00:00+00:00',
        finished_at='2026-10-21T00:00:01+00:00', duration_seconds=1, outcome='success',
    )
    assert SyncState(tmp_path).read()['recent_receipts'][-1]['trigger'] == 'source_change'
    assert read_public_sync_state(state.path)['recent_receipts'][-1]['trigger'] == 'source_change'


def test_sync_receipts_are_bounded_and_sanitized(tmp_path):
    state = SyncState(tmp_path)
    for number in range(25):
        state.record_receipt(
            trigger='interval' if number else 'C:/private/trigger',
            started_at=f'start-{number}', finished_at=f'end-{number}',
            duration_seconds=number, outcome='success',
            error='C:/private/account/message.txt', failed_conversations=number,
        )
    payload = state.read()
    assert len(payload['recent_receipts']) == 20
    assert payload['recent_receipts'][0]['started_at'] == 'start-5'
    encoded = state.path.read_text(encoding='utf-8')
    assert 'private' not in encoded
    assert set(payload['recent_receipts'][-1]) == {
        'trigger', 'started_at', 'finished_at', 'duration_seconds',
        'outcome', 'error', 'failed_conversations',
    }


def test_public_sync_state_is_read_only_and_whitelisted(tmp_path):
    path = tmp_path / 'sync.json'
    path.write_text(json.dumps({
        'state':'partial', 'started_at':'2026-10-17T00:00:00+00:00',
        'finished_at':'2026-10-17T00:01:00+00:00',
        'last_success_at':'2026-10-16T23:55:00+00:00',
        'error':'selected_refresh_partial', 'stage':'complete',
        'stage_started_at':'2026-10-17T00:01:00+00:00',
        'deadline_at':'2026-10-17T00:05:00+00:00',
        'failed_conversations':2, 'archive_success_at':'2026-10-17T00:00:30+00:00',
        'secret_path':'C:/private/account',
        'recent_receipts':[{
            'trigger':'interval', 'started_at':'2026-10-17T00:00:00+00:00',
            'finished_at':'2026-10-17T00:01:00+00:00', 'duration_seconds':60,
            'outcome':'partial', 'error':'selected_refresh_partial',
            'failed_conversations':2, 'private':'message body',
        }],
    }), encoding='utf-8')
    before = path.read_bytes()
    public = read_public_sync_state(path)
    assert path.read_bytes() == before
    assert public['state'] == 'partial'
    assert public['failed_conversations'] == 2
    assert public['recent_receipts'][0]['trigger'] == 'interval'
    encoded = json.dumps(public)
    assert 'private' not in encoded
    assert 'message body' not in encoded
    assert set(public) == {
        'state', 'started_at', 'finished_at', 'last_success_at', 'error',
        'stage', 'stage_started_at', 'deadline_at', 'failed_conversations',
        'archive_success_at', 'recent_receipts',
    }


@pytest.mark.parametrize('receipts', [7, {'private':'message'}, [
    {'trigger':'interval', 'started_at':'2026-10-17T00:00:00+00:00',
     'finished_at':'2026-10-17T00:01:00+00:00', 'duration_seconds':float('nan'),
     'outcome':'success', 'error':None, 'failed_conversations':0},
    {'trigger':'interval', 'started_at':'2026-10-17T00:00:00+00:00',
     'finished_at':'2026-10-17T00:01:00+00:00', 'duration_seconds':float('inf'),
     'outcome':'success', 'error':None, 'failed_conversations':0},
]])
def test_public_sync_state_tolerates_malformed_receipts(tmp_path, receipts):
    path = tmp_path / 'sync.json'
    path.write_text(json.dumps({'state':'ready', 'recent_receipts':receipts}), encoding='utf-8')
    assert read_public_sync_state(path) == {'state':'ready', 'recent_receipts':[]}


def test_deadline_defers_optional_stage_and_preserves_archive_success(tmp_path, monkeypatch):
    import noteford.service as module
    clock = [0.0]
    api = LocalApi({'project_root':str(tmp_path),'index_path':str(tmp_path/'index.db'), 'wechat_root':str(tmp_path/'wechat'),'account':'synthetic','candidates_path':str(tmp_path/'keys.json')})
    def catalog(**kwargs):
        clock[0] = 6.0
        return {'selected_refresh': [], 'archive_status': {'last_success_at': 'archive-success'}}
    monkeypatch.setattr(module, '_refresh_library', catalog)
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module, 'sync_followed_avatars', lambda *a, **k: pytest.fail('avatar stage must be deferred'))
    result = api.refresh(trigger='interval', deadline_monotonic=5.0, deadline_at='deadline')
    state = api.sync_state.read()
    assert result['ok'] is True
    assert result['maintenance_deferred'] == 'avatar_sync'
    assert state['state'] == 'ready'
    assert state['error'] == 'deadline_reached'
    assert state['archive_success_at'] == 'archive-success'
    assert state['stage'] == 'complete'
    assert state['recent_receipts'][-1]['error'] == 'deadline_reached'


def test_status_survives_settings_failure_and_keeps_scheduler_observable(monkeypatch):
    import noteford.service as module
    api = object.__new__(MultiLibraryApi)
    record = SimpleNamespace(id='lib', display_name='Library', state='ready', wechat_root='C:/wechat', account='one')
    api.registry = SimpleNamespace(current=lambda:record)
    api._core = lambda _: SimpleNamespace(status=lambda:{'sync':{'recent_receipts':[]}})
    api._periodic_thread = SimpleNamespace(is_alive=lambda:True)
    api._periodic_stop = threading.Event()
    api._periodic_next_at = 300.0
    api._periodic_error = None
    api._periodic_round_running = False
    api._periodic_trigger = 'retry'
    api.settings = lambda: (_ for _ in ()).throw(ValueError('private value'))
    monkeypatch.setattr(module, 'account_login_status', lambda *a:{'state':'unknown'})
    status = api.status()
    assert status['sync']['scheduler_running'] is True
    assert status['sync']['scheduler_error'] == 'settings_unavailable'
    assert status['sync']['interval_minutes'] is None
    assert status['sync']['next_at'] is not None


def test_stop_timeout_cannot_create_second_worker():
    api = object.__new__(MultiLibraryApi)
    api._periodic_stop = threading.Event()
    worker = SimpleNamespace(is_alive=lambda:True,join=lambda **kwargs:None)
    api._periodic_thread = worker
    api.stop_periodic_refresh()
    api.start_periodic_refresh()
    assert api._periodic_thread is worker
    assert api._periodic_stop.is_set()


def test_message_response_keeps_library_identity_when_selection_changes():
    api=object.__new__(MultiLibraryApi)
    selection=['original']
    api.registry=SimpleNamespace(current=lambda:SimpleNamespace(id=selection[0]))
    def messages(*args,**kwargs):
        selection[0]='other'
        return {'messages':[]}
    api._core=lambda library:SimpleNamespace(messages=messages)
    assert api.messages('conversation')['library_id']=='original'


def test_read_connection_closes_on_success_and_failure(tmp_path):
    import sqlite3
    index=tmp_path/'index.db'
    con=_connect_index(index);ensure_metadata_schema(con);con.commit();con.close()
    repo=WeChatRepository(index,'synthetic')
    for fails in [False,True]:
        try:
            with repo._connect() as connection:
                connection.execute('SELECT 1')
                if fails:raise ValueError('synthetic')
        except ValueError:pass
        with pytest.raises(sqlite3.ProgrammingError):connection.execute('SELECT 1')
