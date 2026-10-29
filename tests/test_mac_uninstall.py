from pathlib import Path
from types import SimpleNamespace
import json
import plistlib
import pytest
from noteford import mac_uninstall as uninstall


def test_owned_paths_only_and_child_symlink_does_not_follow(tmp_path):
    home = tmp_path / 'home'; home.mkdir()
    app = home / 'Applications/Noteford.app'
    paths = uninstall.targets(home, app)
    assert home / 'Library/Caches' / (uninstall.BUNDLE + '.ShipIt') in paths
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
        (path / 'private.db').write_text('test')
    wechat = home / 'Library/Containers/com.tencent.xinWeChat'
    wechat.mkdir(parents=True); (wechat / 'original.db').write_text('original')
    (paths[0] / 'external').symlink_to(wechat, target_is_directory=True)
    exports = home / 'Noteford/export.txt'; exports.write_text('saved')
    uninstall.remove_paths(paths, home)
    assert all(not p.exists() for p in paths)
    assert (wechat / 'original.db').read_text() == 'original'
    assert exports.read_text() == 'saved'


def test_rejects_symlink_ancestor_and_arbitrary_app(tmp_path):
    home = tmp_path / 'home'; home.mkdir()
    with pytest.raises(ValueError): uninstall.targets(home, home)
    with pytest.raises(ValueError): uninstall.targets(home, Path('/Applications/WeChat.app'))
    outside = tmp_path / 'outside'; outside.mkdir()
    (home / 'Noteford').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError): uninstall.targets(home, home / 'Applications/Noteford.app')
    assert outside.exists()


def test_keychain_deletes_all_matching_items_and_no_other_service():
    calls = []; counts = {}
    def run(args, **kwargs):
        service = args[-1]; calls.append(args)
        counts[service] = counts.get(service, 0) + 1
        return SimpleNamespace(returncode=0 if counts[service] < 3 else 44)
    uninstall.clear_credentials(run)
    assert set(counts) == set(uninstall.SERVICES)
    assert all(n == 3 for n in counts.values())
    assert all('-w' not in args for args in calls)
    with pytest.raises(OSError):
        uninstall.clear_credentials(lambda *a, **k: SimpleNamespace(returncode=36))


@pytest.mark.parametrize('credential_error', [False, True])
@pytest.mark.parametrize('main_missing', [False, True])
def test_complete_worker_with_fake_os_preserves_wechat(tmp_path, monkeypatch, credential_error, main_missing):
    home = tmp_path / 'home'; home.mkdir()
    app = home / 'Applications/Noteford.app'
    for p in uninstall.targets(home, app): p.mkdir(parents=True, exist_ok=True)
    companion = app.parent / '卸载 Noteford.app'
    (companion / 'Contents').mkdir()
    (companion / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': uninstall.BUNDLE + '.uninstaller'}))
    root = home / 'Noteford/Data'
    (root / '.product-workspace.json').write_text(json.dumps({'installation': str(root)}))
    (root / 'all-account-database.db').write_text('private')
    if main_missing: app.rmdir()
    original = home / 'WeChat.db'; original.write_text('original')
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    monkeypatch.setattr(uninstall.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(uninstall.sys, 'platform', 'darwin')
    monkeypatch.setattr(uninstall.sys, 'executable', str(companion / 'Contents/Resources/service/noteford-service'))
    monkeypatch.setattr(uninstall.os, 'getppid', lambda: 123)
    monkeypatch.setattr(uninstall, 'alive', lambda pid: False)
    monkeypatch.setattr(uninstall, 'process_ids', lambda app: [])
    def clear():
        if credential_error: raise OSError('denied')
    monkeypatch.setattr(uninstall, 'clear_credentials', clear)
    monkeypatch.setattr(uninstall.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0))
    monkeypatch.setattr(uninstall.tempfile, 'gettempdir', lambda: str(tmp_path))
    notices = []; monkeypatch.setattr(uninstall, 'notify', notices.append)
    assert uninstall.run(['--app', str(app), '--parent', '123']) == int(credential_error)
    assert root.exists() == credential_error and app.exists() == (credential_error and not main_missing)
    assert companion.exists() == credential_error
    assert original.read_text() == 'original'
    assert ('卸载未完成' if credential_error else '已删除') in notices[0]
