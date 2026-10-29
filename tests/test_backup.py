import json
import pytest
from noteford.backup import create, restore, verify
from noteford.runtime_lock import RuntimeLock


def test_backup_restore_and_tamper_rejection(tmp_path):
    root=tmp_path/'data'; root.mkdir()
    (root/'config.local.json').write_text('{}')
    (root/'secrets').mkdir(); (root/'secrets/key').write_bytes(b'synthetic-secret')
    backup=tmp_path/'backup'
    assert create(root,backup)['files'] == 2
    staged=tmp_path/'restored'
    assert restore(backup,staged)['staged_only']
    assert (staged/'secrets/key').read_bytes() == b'synthetic-secret'
    with pytest.raises(ValueError,match='exists'): restore(backup,root)
    (backup/'content/secrets/key').write_bytes(b'corrupted')
    with pytest.raises(ValueError,match='integrity'): restore(backup,tmp_path/'corrupt')
    assert not (tmp_path/'corrupt').exists()


def test_backup_rejects_running_writer_and_incomplete_copy(tmp_path):
    root=tmp_path/'data';root.mkdir();(root/'config.local.json').write_text('{}')
    with RuntimeLock(root):
        with pytest.raises(RuntimeError,match='data_root_in_use'): create(root,tmp_path/'backup')
    assert not (tmp_path/'backup').exists()
    bad=tmp_path/'bad';bad.mkdir()
    (bad/'manifest.json').write_text(json.dumps({'version':1,'files':{'../outside':{}}}))
    with pytest.raises(ValueError,match='manifest'): verify(bad)
