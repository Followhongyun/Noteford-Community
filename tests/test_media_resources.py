import hashlib
import sqlite3
from pathlib import Path

from noteford.media_resources import ImageResourceLookup, read_image_resources, read_video_resources


def resource_db(path, rows):
    con = sqlite3.connect(path)
    con.executescript('CREATE TABLE dir2id(username TEXT); CREATE TABLE image_hardlink_info_v4(md5 TEXT,file_name TEXT,dir1 INTEGER,dir2 INTEGER);')
    con.executemany('INSERT INTO dir2id VALUES(?)', [('a' * 32,), ('2026-09',), ('../../other-account',)])
    con.executemany('INSERT INTO image_hardlink_info_v4 VALUES(?,?,?,?)', rows)
    con.commit(); con.close()


def test_resource_paths_are_exact_and_account_confined(tmp_path):
    db = tmp_path / 'resources.db'
    digest = 'b' * 32
    resource_db(db, [(digest, 'c' * 32 + '_h.dat', 1, 2),
                     (digest, 'c' * 32 + '_t.dat', 1, 2),
                     (digest, 'c' * 32 + '.dat', 3, 2),
                     (digest, '../escape.dat', 1, 2)])
    root = tmp_path / 'acct'
    found = read_image_resources(db, root)
    assert found == {digest: [root / 'msg' / 'attach' / ('a' * 32) / '2026-09' / 'Img' / ('c' * 32 + '_h.dat')]}


def test_lookup_reads_only_target_account_and_direct_image_metadata(tmp_path):
    index = tmp_path / 'index.db'
    con = sqlite3.connect(index)
    con.execute('CREATE TABLE messages(uid TEXT,account TEXT,local_type INTEGER,message_content TEXT)')
    con.executemany('INSERT INTO messages VALUES(?,?,?,?)', [
        ('valid', 'acct', 3, '<msg><img md5="' + 'b' * 32 + '"/></msg>'),
        ('other', 'other', 3, '<msg><img md5="' + 'b' * 32 + '"/></msg>'),
        ('quoted', 'acct', 3, '<msg><quote><img md5="' + 'b' * 32 + '"/></quote></msg>'),
        ('bad', 'acct', 3, '<msg><img md5="../../escape"/></msg>')])
    con.commit(); con.close()
    lookup = ImageResourceLookup(tmp_path / 'acct', index, tmp_path / 'runtime', tmp_path / 'keys')
    # Missing encrypted resource DB is an honest miss, not a message read failure.
    assert lookup.find_image('valid') == ('b' * 32, [])
    for uid in ('other', 'quoted', 'bad', 'absent'):
        assert lookup.find_image(uid) == (None, [])

def test_failed_resource_refresh_discards_stale_paths_and_cleans_snapshot(tmp_path):
    lookup = ImageResourceLookup(tmp_path / 'acct', tmp_path / 'index', tmp_path / 'runtime', tmp_path / 'missing-keys')
    lookup._resources = {'b' * 32: [tmp_path / 'stale']}
    lookup._refresh()
    assert lookup._resources == {}
    lookup.source.parent.mkdir(parents=True)
    lookup.source.write_bytes(b'invalid encrypted database')
    lookup._resources = {'b' * 32: [tmp_path / 'stale']}
    lookup._refresh()
    assert lookup._resources == {}
    assert not list(lookup.runtime.glob('image-resource-*'))
    assert lookup.source.read_bytes() == b'invalid encrypted database'


def test_video_resource_aliases_exclude_thumbnails_and_unsafe_paths(tmp_path):
    db = tmp_path / 'resources.db'
    resource_db(db, [])
    con = sqlite3.connect(db)
    con.execute('CREATE TABLE video_hardlink_info_v4(md5 TEXT,file_name TEXT,dir1 INTEGER)')
    digest = 'b' * 32
    con.executemany('INSERT INTO video_hardlink_info_v4 VALUES(?,?,?)', [
        (digest, 'c' * 32 + '.mp4', 2), (digest, 'c' * 32 + '.jpg', 2),
        (digest, '../escape.mp4', 2), (digest, 'c' * 32 + '.mp4', 3)])
    con.commit(); con.close()
    root = tmp_path / 'acct'
    assert read_video_resources(db, root) == {digest: [root / 'msg/video/2026-09' / ('c' * 32 + '.mp4')]}


def test_video_lookup_only_reads_direct_same_account_metadata(tmp_path):
    index = tmp_path / 'index.db'
    con = sqlite3.connect(index)
    con.execute('CREATE TABLE messages(uid TEXT,account TEXT,local_type INTEGER,message_content TEXT)')
    xml = '<msg><videomsg md5="' + 'b' * 32 + '"/></msg>'
    con.executemany('INSERT INTO messages VALUES(?,?,?,?)', [
        ('valid', 'acct', 43, xml), ('other', 'other', 43, xml),
        ('image', 'acct', 3, xml), ('nested', 'acct', 43, '<msg><quote>' + xml + '</quote></msg>')])
    con.commit(); con.close()
    lookup = ImageResourceLookup(tmp_path / 'acct', index, tmp_path / 'runtime', tmp_path / 'keys')
    assert lookup.find_video('valid') == ('b' * 32, [])
    for uid in ('other', 'image', 'nested'):
        assert lookup.find_video(uid) == (None, [])
