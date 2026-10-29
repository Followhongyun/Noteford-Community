import json
import sqlite3

import pytest

from noteford.assets import AvatarResolver, download_avatar, sync_followed_avatars


def test_sync_retries_missing_and_isolates_followed_accounts(tmp_path):
    index = tmp_path / 'index.db'
    with sqlite3.connect(index) as con:
        con.executescript('''
            CREATE TABLE conversation_selection(account,username,chat_table,state);
            CREATE TABLE messages(account,chat_table,source_db,real_sender_id);
            CREATE TABLE sender_map(account,source_db,sender_rowid,username);
            INSERT INTO conversation_selection VALUES('a','group','t','following'),('a','paused','p','paused'),('b','other','b','following');
            INSERT INTO messages VALUES('a','t','m',1),('a','p','m',2),('b','b','m',3);
            INSERT INTO sender_map VALUES('a','m',1,'member'),('a','m',2,'ignored'),('b','m',3,'othermember');
        ''')
    contact = tmp_path / 'contact.db'
    with sqlite3.connect(contact) as con:
        con.execute('CREATE TABLE contact(username,small_head_url,big_head_url)')
        con.executemany('INSERT INTO contact VALUES(?,?,?)', [(name, 'https://wx.qlogo.cn/' + name, '') for name in ['group', 'member', 'ignored', 'othermember']])
    state = tmp_path / 'data/state/a/contact/contact.json'
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({'decrypted': str(contact)}))
    resolver = AvatarResolver(tmp_path, 'a', tmp_path / 'cache')
    calls = []
    def fail(url):
        calls.append(url)
        raise OSError('offline')
    first = sync_followed_avatars(index, resolver, fetch=fail)
    assert first['checked'] == first['missing'] == 2
    assert set(calls) == {'https://wx.qlogo.cn/group', 'https://wx.qlogo.cn/member'}
    data = b'\xff\xd8\xff\xe0fixture\xff\xd9'
    second = sync_followed_avatars(index, resolver, fetch=lambda url: data)
    assert second['recovered'] == 2 and second['missing'] == 0
    assert len(resolver.export_many(['group', 'member'])) == 2
    third = sync_followed_avatars(index, resolver, fetch=fail)
    assert third['available'] == 2 and third['recovered'] == 0
    assert len(calls) == 2  # Existing avatars do not cause another network request.
    assert json.loads((tmp_path / 'data/state/avatar_sync.json').read_text())['missing'] == 0


@pytest.mark.parametrize('url', ['https://example.com/a', 'http://127.0.0.1/a', 'file:///a', 'https://wx.qlogo.cn.evil.test/a', 'https://user:pass@wx.qlogo.cn/a', 'https://wx.qlogo.cn:1234/a'])
def test_avatar_fetch_rejects_non_avatar_targets(url):
    with pytest.raises(ValueError):
        download_avatar(url)
