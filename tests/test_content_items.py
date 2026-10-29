from types import SimpleNamespace
import pytest
from noteford.content_items import extract_items, list_materials
from noteford.decode import DecodedContent

def d(kind='text', text='', url=None, title=None):
    return DecodedContent(1,0,kind,text,url=url,title=title)

def test_multiple_urls_and_exact_article_host():
    items=extract_items(d(text='https://mp.weixin.qq.com/s/a https://example.com/a https://mp.weixin.qq.com.evil.test/a'))
    assert [i[0] for i in items]==['article','link','link']
    assert len(extract_items(d(text='https://example.com/a',url='https://example.com/a')))==1

def test_channel_without_url_preserved():
    assert extract_items(d('channel_video',text='讲解'))==[('channel','','讲解')]

@pytest.mark.parametrize('kind',['image','video','file'])
def test_attachment_without_local_file_not_dropped(kind):
    assert extract_items(d(kind))[0][0]==kind

def test_unsafe_card_url_not_exposed():
    assert extract_items(d(url='javascript:alert(1)'))==[]

def test_invalid_category_rejected_before_database_access():
    with pytest.raises(ValueError):list_materials(None,category='anything')
    with pytest.raises(ValueError):list_materials(None,offset=-1)

@pytest.fixture
def material_repository(tmp_path):
    import sqlite3
    from contextlib import contextmanager
    path=tmp_path/'fixture.db'
    c=sqlite3.connect(path)
    c.executescript('''CREATE TABLE messages(account,chat_table,uid,local_type,message_content,compress_content,packed_info_data,source_db,real_sender_id,create_time);
    CREATE TABLE conversations(account,chat_table,username,display_name,is_group);
    CREATE TABLE conversation_selection(account,username,state);
    CREATE TABLE sender_map(account,source_db,sender_rowid,username);
    CREATE TABLE contacts(account,username,display_name);
    CREATE TABLE media_exports(uid,status);
    INSERT INTO conversations VALUES('a','t','u','会话',0),('a','hidden','h','未关注',0);
    INSERT INTO conversation_selection VALUES('a','u','following');
    INSERT INTO messages VALUES('a','t','one',1,'https://example.com/a',NULL,NULL,'db',1,2),('a','t','two',1,'https://example.com/a https://example.com/b',NULL,NULL,'db',1,1),('a','hidden','three',1,'https://secret.test/',NULL,NULL,'db',1,3);''');c.commit();c.close()
    @contextmanager
    def connect():
        c=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True);c.row_factory=sqlite3.Row
        try:yield c
        finally:c.close()
    return SimpleNamespace(account='a', index_path=path, _connect=connect)

def test_deduplication_retains_sources_and_pagination(material_repository):
    import sqlite3
    repo = material_repository
    path = repo.index_path
    first=list_materials(repo,limit=1)
    assert first['total']==2 and len(first['items'][0]['sources'])==2
    assert first['next_offset']==1
    second=list_materials(repo,offset=1,limit=1)
    assert second['next_offset'] is None and second['items'][0]['url']=='https://example.com/b'
    assert list_materials(repo,query='secret')['total']==0

    # Source filtering applies before deduplication, counts and pagination.
    selected=list_materials(repo, allowed_conversations=['u'], limit=1)
    assert selected['total']==2 and selected['counts']['link']==2
    assert all(s['conversation_id']=='u' for item in selected['items'] for s in item['sources'])
    assert list_materials(repo, allowed_conversations=['h'])['total']==0
    assert list_materials(repo, allowed_conversations=['missing'])['next_offset'] is None

    # Mixed media is filtered before pagination and retains chronological order.
    c=sqlite3.connect(path)
    c.executemany('INSERT INTO messages VALUES(?,?,?,?,?,NULL,NULL,?,?,?)', [
        ('a','t','photo',3,'','db',1,6),
        ('a','t','clip',43,'','db',1,5),
        ('a','hidden','private-photo',3,'','db',1,7),
    ])
    c.commit();c.close()
    media=list_materials(repo,category='media',limit=1)
    assert media['total']==2 and media['items'][0]['category']=='image'
    assert media['next_offset']==1
    next_media=list_materials(repo,category='media',offset=1,limit=1)
    assert next_media['items'][0]['category']=='video' and next_media['next_offset'] is None
    assert list_materials(repo,category='image')['total']==1
    assert list_materials(repo,category='media',allowed_conversations=['h'])['total']==0
    assert list_materials(repo,category='media',query='no-match')['total']==0


def test_snapshot_reuses_scan_and_invalidates_wal_and_selection(material_repository, monkeypatch):
    import sqlite3
    import noteford.content_items as content
    repo = material_repository
    scans = []
    decoded = []
    collect = content._collect_materials
    decode = content.decode_message
    monkeypatch.setattr(content, '_collect_materials', lambda *args: (scans.append(1), collect(*args))[1])
    monkeypatch.setattr(content, 'decode_message', lambda *args: (decoded.append(1), decode(*args))[1])
    with content._cache_lock:
        content._snapshots.clear()
        content._decoded.clear()
    writer = sqlite3.connect(repo.index_path)
    writer.execute('PRAGMA journal_mode=WAL')
    try:
        first = list_materials(repo, limit=1)
        first['items'][0]['sources'].clear()  # callers cannot mutate shared cache
        assert len(list_materials(repo, category='link')['items'][0]['sources']) == 2
        assert list_materials(repo, offset=1, limit=1)['items']
        assert len(scans) == 1 and len(decoded) == 2
        writer.execute("INSERT INTO messages VALUES('a','t','new',1,'https://example.com/new',NULL,NULL,'db',1,9)")
        writer.commit()
        assert list_materials(repo)['total'] == 3
        assert len(scans) == 2 and len(decoded) == 3
        writer.execute("UPDATE messages SET message_content='https://example.com/edited' WHERE uid='new'")
        writer.commit()
        assert list_materials(repo)['items'][0]['url'] == 'https://example.com/edited'
        writer.execute("UPDATE conversation_selection SET state='excluded'")
        writer.commit()
        assert list_materials(repo)['total'] == 0
    finally:
        writer.close()


def test_cache_keeps_authorization_and_filters_separate(material_repository):
    repo = material_repository
    assert list_materials(repo, include_unselected=True)['total'] == 3
    assert list_materials(repo)['total'] == 2
    assert list_materials(repo, allowed_conversations=[])['total'] == 0
    assert list_materials(repo, allowed_conversations=['h'])['total'] == 0
    assert list_materials(repo, include_unselected=True, allowed_conversations=['h'])['total'] == 1
    assert list_materials(repo, from_timestamp=2)['total'] == 1
    assert list_materials(repo, to_timestamp=2)['total'] == 2
    assert list_materials(repo, query='example.com/b')['total'] == 2
    other = SimpleNamespace(account='other', index_path=repo.index_path, _connect=repo._connect)
    assert list_materials(other)['total'] == 0


def test_cache_isolated_by_database_path_and_replacement(material_repository, tmp_path):
    import sqlite3
    import shutil
    from noteford.repository import WeChatRepository
    original = material_repository
    assert list_materials(original)['total'] == 2
    alternate = tmp_path / 'other.db'
    shutil.copy2(original.index_path, alternate)
    connection = sqlite3.connect(alternate)
    connection.execute("UPDATE messages SET message_content='https://other.example/' WHERE account='a'")
    connection.commit()
    connection.close()
    other = WeChatRepository(alternate, 'a')
    assert list_materials(other)['total'] == 1
    assert list_materials(original)['total'] == 2
    alternate.replace(original.index_path)
    assert list_materials(original)['total'] == 1
    assert list_materials(original)['items'][0]['url'] == 'https://other.example/'


def test_message_link_cards_do_not_merge_and_paginate_by_time(material_repository):
    import sqlite3
    repo = material_repository
    cards = list_materials(repo, category='link', link_messages=True, limit=1)
    assert cards['total'] == 2
    assert cards['items'][0]['sources'][0]['message_uid'] == 'one'
    older = list_materials(repo, category='link', link_messages=True, offset=1, limit=1)
    assert older['items'][0]['sources'][0]['message_uid'] == 'two'
    assert cards['items'][0]['url'] == older['items'][0]['url']
    assert cards['items'][0]['id'] != older['items'][0]['id']
    assert len(older['items'][0]['sources']) == 1
    # Two URLs in a message still produce one message card; both remain in text.
    assert 'https://example.com/b' in older['items'][0]['text']
    # Existing MCP/Windows aggregate behavior uses a separate cache entry.
    assert len(list_materials(repo)['items'][0]['sources']) == 2
    with sqlite3.connect(repo.index_path) as con:
        con.execute("INSERT INTO messages VALUES('a','t','newest',1,'https://example.com/a',NULL,NULL,'db',1,99)")
    updated = list_materials(repo, category='link', link_messages=True)
    assert updated['total'] == 3
    assert [item['sources'][0]['message_uid'] for item in updated['items']] == ['newest', 'one', 'two']
    assert all(len(item['sources']) == 1 for item in updated['items'])
    assert list_materials(repo, category='link', link_messages=True, allowed_conversations=['h'])['total'] == 0
