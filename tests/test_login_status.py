from types import SimpleNamespace
from noteford.login_status import LoginDetector, _path


def pair(account='one', root='C:/wechat'):
    return {_path(f'{root}/{account}/db_storage/{name}/{name}.db') for name in ['session', 'contact']}


def test_exact_account_and_source_root_binding():
    detector = LoginDetector(lambda: ([pair()], False))
    assert detector.check('C:/wechat', 'one')['state'] == 'active'
    for root, account in [('C:/wechat', 'one_backup'), ('C:/wechat', 'two'), ('D:/wechat', 'one')]:
        assert detector.check(root, account)['state'] == 'unknown'
    assert detector.check('c:/WECHAT', 'ONE')['state'] == 'active'


def test_process_presence_missing_handles_and_access_denial_are_not_login():
    for files, incomplete, expected in [([], False, 'not_running'), ([], True, 'unknown'), ([set()], False, 'unknown'), ([set()], True, 'unknown')]:
        value = LoginDetector(lambda: (files, incomplete)).check('C:/wechat', 'one')
        assert value['state'] == expected
        assert value['network_online'] is None
        assert set(value) == {'state', 'reason', 'checked_at', 'network_online'}
    a,b = pair()
    assert LoginDetector(lambda: ([{a},{b}], False)).check('C:/wechat', 'one')['state'] == 'unknown'


def test_cache_expires_and_account_switch_never_reuses_another_accounts_result():
    clock=[0];calls=[];files=[pair()]
    def inventory():calls.append(1);return files,False
    detector=LoginDetector(inventory,lambda:clock[0])
    assert detector.check('C:/wechat','one')['state']=='active'
    assert detector.check('C:/wechat','two')['state']=='unknown'
    assert len(calls)==1
    files.clear();clock[0]=11
    assert detector.check('C:/wechat','one')['state']=='not_running'
    assert len(calls)==2


def test_status_uses_captured_library_identity(monkeypatch):
    import threading
    import noteford.service as service
    record=SimpleNamespace(id='one',wechat_root='C:/wechat',account='one',display_name='One',state='ready')
    api=object.__new__(service.MultiLibraryApi)
    api.registry=SimpleNamespace(current=lambda:record)
    api._core=lambda _:SimpleNamespace(status=lambda:{'sync':{}})
    api._periodic_thread=None;api._periodic_stop=threading.Event();api.root_config={}
    seen=[]
    monkeypatch.setattr(service,'account_login_status',lambda root,account:seen.append((root,account)) or {'state':'active'})
    value=api.status()
    assert seen==[('C:/wechat','one')]
    assert value['library_id']=='one' and value['wechat_login']['state']=='active'


def test_login_reconnect_is_account_bound_and_debounced():
    from noteford.login_status import LoginReconnect
    now = [0]
    states = {'one': 'not_running', 'two': 'active'}
    watcher = LoginReconnect(lambda root, account: {'state': states[account]}, lambda: now[0])
    records = [SimpleNamespace(wechat_root='C:/wechat', account=a) for a in states]
    assert not watcher.poll(records)  # Startup handled by normal refresh.
    states['one'] = 'active'; now[0] = 10
    assert watcher.poll(records)
    assert not watcher.poll(records)  # No repeated refresh while still logged in.
    states['one'] = 'unknown'; now[0] = 20
    assert not watcher.poll(records)
    states['one'] = 'active'; now[0] = 30
    assert not watcher.poll(records)  # Ignore transient enumeration noise.
    states['one'] = 'not_running'; now[0] = 80
    assert not watcher.poll(records)
    states['one'] = 'active'; now[0] = 90
    assert watcher.poll(records)
    assert not watcher.poll([])
    assert not watcher.poll(records)  # Newly registered account gets a baseline.
