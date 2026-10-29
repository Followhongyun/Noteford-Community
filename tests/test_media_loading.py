import sqlite3
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import json
import threading
import pytest
from noteford.media_loading import ensure_policy, set_enabled, allows_original, mark_manual
from noteford.media import MediaResolver
from noteford.service import MultiLibraryApi


def policy_index(path):
    with sqlite3.connect(path) as con:
        con.execute('CREATE TABLE messages(uid TEXT PRIMARY KEY, local_type INTEGER)')
        con.execute("INSERT INTO messages VALUES('legacy',3)")
        con.commit()
        ensure_policy(con)
    return path


def insert(path, uid):
    with sqlite3.connect(path) as con:
        con.execute('INSERT INTO messages VALUES(?,43)',(uid,))


def test_first_sync_choice_survives_toggles_updates_and_restart(tmp_path):
    path=policy_index(tmp_path/'index')
    insert(path,'on')
    set_enabled(path,False); insert(path,'off')
    set_enabled(path,True); insert(path,'on-again')
    with sqlite3.connect(path) as con:
        con.execute("UPDATE messages SET local_type=43 WHERE uid='off'")
        con.commit(); ensure_policy(con)
    assert allows_original(path,'legacy') and allows_original(path,'on')
    assert not allows_original(path,'off')
    assert allows_original(path,'on-again')
    mark_manual(path,'off');set_enabled(path,False)
    assert allows_original(path,'off')
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT automatic,manual FROM message_media_policy WHERE uid='off'").fetchone()==(0,1)


def test_thumbnail_mode_never_copies_local_original_and_manual_load_is_retained(tmp_path):
    path=policy_index(tmp_path/'index');set_enabled(path,False);insert(path,'off')
    resolver=MediaResolver(tmp_path/'wechat','acct')
    resolver.original_allowed=lambda uid:allows_original(path,uid)
    stamp=int(datetime(2026,9,21).timestamp())
    img=tmp_path/'wechat/acct/msg/attach/hash/2026-09/Img';img.mkdir(parents=True)
    (img/'abc_t.dat').write_bytes(b'\xff\xd8\xffthumb');(img/'abc_h.dat').write_bytes(b'\xff\xd8\xfffull')
    video=tmp_path/'wechat/acct/msg/video/2026-09';video.mkdir(parents=True)
    (video/'abc.jpg').write_bytes(b'thumb');(video/'abc.mp4').write_bytes(b'full-video')
    cache=tmp_path/'cache'
    assert resolver.export_image('Msg_hash',stamp,'abc','off',cache).status=='ready_thumbnail'
    assert resolver.export_video(stamp,'abc','off',cache).status=='thumbnail_only'
    assert not list(cache.rglob('*.mp4'))
    mark_manual(path,'off')
    full=resolver.export_video(stamp,'abc','off',cache)
    assert full.status=='ready'
    set_enabled(path,True);set_enabled(path,False)
    assert resolver.export_video(stamp,'abc','off',cache).status=='ready'
    assert Path(full.path).read_bytes()==b'full-video'


def test_global_setting_persists_and_rejects_invalid_values(tmp_path):
    paths=[policy_index(tmp_path/'one'),policy_index(tmp_path/'two')]
    cores=[SimpleNamespace(config={'index_path':str(p)}) for p in paths]
    api=MultiLibraryApi.__new__(MultiLibraryApi);api._lock=threading.RLock();api.root_config={}
    api.config_path=tmp_path/'config.json';api.config_path.write_text('{}')
    api.registry=SimpleNamespace(list=lambda:[SimpleNamespace(id=i) for i in range(2)])
    api._core=lambda i:cores[i]
    assert api.update_auto_media_loading(False)['auto_media_loading'] is False
    for p in paths:insert(p,'new');assert not allows_original(p,'new')
    assert json.loads(api.config_path.read_text())['auto_media_loading'] is False
    for value in [None,0,1,'false']:
        with pytest.raises(ValueError):api.update_auto_media_loading(value)
    assert api.update_auto_media_loading(True)['auto_media_loading'] is True
    for p in paths:assert not allows_original(p,'new')


def test_existing_policy_upgrade_preserves_choices_and_cache_references(tmp_path):
    path = tmp_path/'old-index'
    with sqlite3.connect(path) as con:
        con.executescript("""
            CREATE TABLE messages(uid TEXT PRIMARY KEY, local_type INTEGER);
            CREATE TABLE media_loading_setting(id INTEGER PRIMARY KEY, enabled INTEGER);
            INSERT INTO media_loading_setting VALUES(1,0);
            CREATE TABLE message_media_policy(uid TEXT PRIMARY KEY, automatic INTEGER, manual INTEGER DEFAULT 0);
            INSERT INTO messages VALUES('old',3);
            INSERT INTO message_media_policy VALUES('old',0,1);
            CREATE TRIGGER capture_media_loading AFTER INSERT ON messages BEGIN
              INSERT OR IGNORE INTO message_media_policy(uid,automatic) SELECT NEW.uid,enabled FROM media_loading_setting WHERE id=1;
            END;
        """)
        ensure_policy(con)
        ensure_policy(con)
        assert con.execute("SELECT automatic,manual,range_exempt FROM message_media_policy WHERE uid='old'").fetchone() == (0,1,0)
        con.execute("INSERT INTO messages VALUES('new',3)")
        assert con.execute("SELECT automatic,manual,range_exempt FROM message_media_policy WHERE uid='new'").fetchone() == (0,0,1)
    assert allows_original(path,'old')
    assert not allows_original(path,'new')
