import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from noteford.assets import AvatarResolver, latest_decrypted_database
from noteford.decrypted_archive import refresh_decrypted_library, scan_decrypted_archives
from noteford.mac_metadata import avatar_fields, avatar_url, group_members, room_nicknames
from noteford.repository import WeChatRepository


def field(number, value):
    data = value.encode() if isinstance(value, str) else value
    length = len(data)
    encoded = bytearray()
    while length > 127:
        encoded.append((length & 127) | 128)
        length >>= 7
    return bytes([number * 8 + 2]) + bytes(encoded) + bytes([length]) + data


def fixture_archive(tmp_path):
    archive = tmp_path / 'archive'
    account = 'wxid_me_ab12'
    root = archive / account
    root.mkdir(parents=True)
    contact = root / 'contact.db'
    with sqlite3.connect(contact) as con:
        con.executescript('''
            CREATE TABLE contact(id INTEGER, username, alias, remark, nick_name,
                                 small_head_url, big_head_url);
            CREATE TABLE chat_room(id INTEGER, username, owner, ext_buffer BLOB);
            CREATE TABLE chatroom_member(room_id INTEGER, member_id INTEGER);
            INSERT INTO contact VALUES(1,'wxid_me','','','本人','','');
            INSERT INTO contact VALUES(2,'member','','','微信昵称','http://wx.qlogo.cn/member','');
            INSERT INTO contact VALUES(3,'friend','','我的备注','好友昵称','','');
            INSERT INTO contact VALUES(4,'room@chatroom','','','测试群','','');
            INSERT INTO contact VALUES(5,'other@chatroom','','','另一个群','','');
            INSERT INTO chatroom_member VALUES(1,1),(1,2),(1,3),(2,2);
        ''')
        blob = field(1, field(1, 'member') + field(2, '群内昵称'))
        blob += field(1, field(1, 'friend') + field(2, '不覆盖备注'))
        con.execute('INSERT INTO chat_room VALUES(1,?,?,?)', ('room@chatroom', 'member', blob))
        con.execute('INSERT INTO chat_room VALUES(2,?,?,?)', ('other@chatroom', 'member', field(1, field(1, 'member') + field(2, '另一群昵称'))))
    with sqlite3.connect(root / 'session.db') as con:
        con.executescript('''
            CREATE TABLE SessionTable(username,type,last_timestamp,sort_timestamp);
            CREATE TABLE SessionNoContactInfoTable(username,session_title);
            INSERT INTO SessionTable VALUES('room@chatroom',2,100,100),('other@chatroom',2,90,90);
        ''')
    with sqlite3.connect(root / 'message_0.db') as con:
        con.executescript("CREATE TABLE Name2Id(user_name); INSERT INTO Name2Id VALUES('member'),('wxid_me');")
        table = 'Msg_' + hashlib.md5(b'room@chatroom').hexdigest()
        con.execute(f'CREATE TABLE {table}(local_id,server_id,create_time,local_type,message_content,real_sender_id)')
        con.execute(f'INSERT INTO {table} VALUES(1,101,100,1,?,1)', ('hello',))
        con.execute(f'INSERT INTO {table} VALUES(2,102,101,1,?,2)', ('reply',))
    image = b'\x89PNG\r\n\x1a\nfixture image'
    with sqlite3.connect(root / 'head_image.db') as con:
        con.execute('CREATE TABLE head_image(username,md5,image_buffer,update_time)')
        con.execute('INSERT INTO head_image VALUES(?,?,?,1)', ('wxid_me', 'v1', image))
    return archive, account, image


def refresh(archive, account, project):
    return refresh_decrypted_library(wechat_root=archive, account=account, project_root=project,
                                    candidates_path=project/'unused', index_path=project/'index.db')


def test_catalog_readable_before_messages_and_unfollowed_history_is_indexed(tmp_path, monkeypatch):
    from noteford import indexer
    archive, account, _ = fixture_archive(tmp_path)
    project = tmp_path / 'progressive'
    original = indexer.index_messages
    observed = []
    def inspect_then_index(**kwargs):
        with sqlite3.connect(kwargs['index_path']) as con:
            assert con.execute('SELECT COUNT(*) FROM conversations').fetchone()[0] >= 2
            assert con.execute('SELECT COUNT(*) FROM conversation_selection').fetchone()[0] == 0
            observed.append(con.execute('SELECT COUNT(*) FROM messages').fetchone()[0])
        return original(**kwargs)
    monkeypatch.setattr(indexer, 'index_messages', inspect_then_index)
    refresh(archive, account, project)
    assert observed == [0]
    with sqlite3.connect(project / 'index.db') as con:
        assert con.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 2
        assert con.execute('SELECT COUNT(*) FROM conversation_selection').fetchone()[0] == 0


def test_progressive_catalog_does_not_claim_complete_archive(tmp_path):
    archive, account, _ = fixture_archive(tmp_path)
    project = tmp_path / 'progressive'
    refresh_decrypted_library(wechat_root=archive, account=account, project_root=project,
        candidates_path=project/'unused', index_path=project/'index.db',
        message_database_names=set(), write_archive_status=False)
    with sqlite3.connect(project / 'index.db') as con:
        assert con.execute('SELECT COUNT(*) FROM conversations').fetchone()[0] >= 2
        assert con.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 0
    assert not (project / 'data/state/hourly_refresh.json').exists()
    refresh(archive, account, project)
    assert (project / 'data/state/hourly_refresh.json').is_file()


def test_mac_profiles_members_and_senders_are_readonly_and_account_scoped(tmp_path, monkeypatch):
    monkeypatch.setattr('noteford.platform_seam.library_source_kind', lambda platform=None: 'decrypted_archive')
    archive, account, image = fixture_archive(tmp_path)
    project = tmp_path / 'lib-fixture'
    project.mkdir()
    before = {p: p.read_bytes() for p in archive.rglob('*.db')}
    result = refresh(archive, account, project)
    assert result['catalog']['metadata']['sender_mappings'] == 2
    assert latest_decrypted_database(project, account, 'contact', 'contact') == archive/account/'contact.db'
    repo = WeChatRepository(project/'index.db', account)
    messages = repo.list_messages('room@chatroom')['messages']
    assert [m['sender']['name'] for m in messages] == ['群内昵称', '本人']
    assert [m['sender']['from_me'] for m in messages] == [False, True]
    assert repo.message_by_uid(messages[0]['uid'])['message']['sender']['name'] == '群内昵称'
    assert len(repo.search_messages('群内昵称')['items']) == 1
    assert len(repo.list_messages('room@chatroom', query='群内昵称')['messages']) == 1
    members = {r['username']: r for r in repo.group_members('room@chatroom', archive/account/'contact.db')}
    assert members['member']['name'] == '群内昵称'
    assert members['member']['is_owner'] == 1
    assert members['friend']['name'] == '我的备注'
    assert group_members(project/'index.db', account, 'other@chatroom')[0]['name'] == '另一群昵称'
    assert group_members(project/'index.db', 'another-account') == []
    resolver = AvatarResolver(project, account, project/'data/media-cache')
    local = resolver.export_many(['wxid_me', 'member'])
    assert Path(local['wxid_me']).read_bytes() == image
    fields = avatar_fields(resolver, ['wxid_me','member','missing'], local)
    assert fields['member']['avatar_remote_url'] == 'https://wx.qlogo.cn/member'
    assert fields['member']['avatar_library_id'] == 'lib-fixture'
    assert 'avatar_remote_url' not in fields['wxid_me']
    candidate = scan_decrypted_archives([archive])[0]
    assert candidate['display_name'] == '本人' and candidate['preview_status'] == 'ready'
    assert candidate['avatar_data_url'].startswith('data:image/png;base64,')
    assert {p: p.read_bytes() for p in archive.rglob('*.db')} == before


def test_mac_profile_changes_and_member_removal_survive_reopening(tmp_path, monkeypatch):
    monkeypatch.setattr('noteford.platform_seam.library_source_kind', lambda platform=None: 'decrypted_archive')
    archive, account, _ = fixture_archive(tmp_path)
    project = tmp_path/'lib-fixture'; project.mkdir()
    refresh(archive, account, project)
    with sqlite3.connect(archive/account/'contact.db') as con:
        con.execute("UPDATE contact SET remark='更新后的备注' WHERE username='member'")
        con.execute('DELETE FROM chatroom_member WHERE room_id=1 AND member_id=3')
    updated = b'\x89PNG\r\n\x1a\nnew avatar'
    with sqlite3.connect(archive/account/'head_image.db') as con:
        con.execute('UPDATE head_image SET image_buffer=?,md5=?', (updated, 'v2'))
    refresh(archive, account, project)
    assert [r['name'] for r in group_members(project/'index.db', account, 'room@chatroom')] == ['更新后的备注', '本人']
    path = AvatarResolver(project, account, project/'data/media-cache').export_many(['wxid_me'])['wxid_me']
    assert Path(path).read_bytes() == updated
    assert WeChatRepository(project/'index.db', account).list_messages('room@chatroom')['messages'][0]['sender']['name'] == '更新后的备注'


def test_missing_self_contact_does_not_borrow_another_person_name(tmp_path):
    archive, account, _ = fixture_archive(tmp_path)
    with sqlite3.connect(archive/account/'contact.db') as con:
        con.execute("DELETE FROM contact WHERE username='wxid_me'")
    assert scan_decrypted_archives([archive])[0]['display_name'] == 'wxid_me'


@pytest.mark.parametrize('url', ['file:///etc/passwd','https://wx.qlogo.cn.evil.test/a',
    'https://user:password@wx.qlogo.cn/a','https://wx.qlogo.cn:444/a','https://127.0.0.1/a'])
def test_mac_avatar_fallback_rejects_untrusted_targets(url):
    assert avatar_url(url) is None


def test_malformed_member_names_do_not_invent_an_identity():
    assert room_nicknames(b'\x0a\xff') == {}
    assert room_nicknames(field(1, field(1,b'\xff') + field(2,'bad'))) == {}


def test_windows_keeps_existing_contact_names_even_with_mac_fixture(tmp_path, monkeypatch):
    archive, account, _ = fixture_archive(tmp_path)
    project = tmp_path/'lib-fixture'; project.mkdir()
    refresh(archive, account, project)
    monkeypatch.setattr('noteford.platform_seam.library_source_kind', lambda platform=None: 'wechat_files')
    repo = WeChatRepository(project/'index.db', account)
    assert repo.list_messages('room@chatroom')['messages'][0]['sender']['name'] == '微信昵称'
    assert {r['username']:r['name'] for r in repo.group_members('room@chatroom', archive/account/'contact.db')}['member'] == '微信昵称'


def test_mac_address_book_is_not_limited_to_recent_sessions_or_500_rows(tmp_path, monkeypatch):
    monkeypatch.setattr('noteford.platform_seam.library_source_kind', lambda platform=None: 'decrypted_archive')
    archive, account, _ = fixture_archive(tmp_path)
    source = archive/account/'contact.db'
    with sqlite3.connect(source) as con:
        con.executescript('ALTER TABLE contact ADD COLUMN local_type INTEGER DEFAULT 3; '
                          'ALTER TABLE contact ADD COLUMN delete_flag INTEGER DEFAULT 0; '
                          'ALTER TABLE contact ADD COLUMN verify_flag INTEGER DEFAULT 0;')
        con.execute("UPDATE contact SET local_type=1 WHERE username IN ('wxid_me','friend')")
        con.executemany('INSERT INTO contact(username,nick_name,local_type,delete_flag,verify_flag) VALUES(?,?,?,?,?)',
                        [(f'friend_{n:04}', f'好友{n:04}', 1, 0, 0) for n in range(505)] + [
                            ('legacy_official', '公众号', 1, 0, 8),
                            ('colleague@openim', '企业联系人', 5, 0, 0),
                            ('idle@chatroom', '无近期消息的群', 2, 0, 0),
                            ('cached', '仅群成员缓存', 3, 0, 0),
                            ('deleted', '已删除', 1, 1, 0)])
    before = source.read_bytes()
    project = tmp_path/'lib-fixture'; project.mkdir()
    refresh(archive, account, project)
    repo = WeChatRepository(project/'index.db', account)
    first = repo.list_conversations(limit=500)
    rest = repo.list_conversations(limit=500, offset=500)
    rows = {r['username']: r for r in first + rest}
    assert len(first) == 500 and len(rows) == 511
    assert not ({'wxid_me', 'cached', 'deleted', 'member'} & rows.keys())
    assert rows['friend']['message_count'] == 0 and not rows['friend']['has_local_messages']
    assert repo.list_messages('friend')['messages'] == []
    assert rows['legacy_official']['contact_kind'] == 'official'
    assert rows['colleague@openim']['contact_kind'] == 'people'
    assert rows['idle@chatroom']['contact_kind'] == 'groups'
    assert repo.list_conversations(query='好友0504')[0]['username'] == 'friend_0504'
    assert source.read_bytes() == before
    with sqlite3.connect(source) as con:
        con.execute("UPDATE contact SET delete_flag=1 WHERE username='friend_0504'")
    refresh(archive, account, project)
    assert repo.list_conversations(query='好友0504') == []
