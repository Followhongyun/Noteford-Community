import sqlite3
from contextlib import closing

from noteford.mac_special import MacPaymentStateReader, refresh_solitaire
from noteford.mac_metadata import register_sources
from noteford.solitaire import read_memberships
from noteford.mac_decode import decode_message


def test_mac_payment_snapshot_updates_and_preserves_account_and_message_identity(tmp_path):
    project = tmp_path/'project'
    def snapshot(name, received):
        source = tmp_path/name
        with closing(sqlite3.connect(source)) as con, con:
            con.execute('CREATE TABLE redEnvelopeTable(session_name,message_server_id,hb_status,receive_status,hb_type)')
            con.executemany('INSERT INTO redEnvelopeTable VALUES(?,?,?,?,?)', [
                ('chat-a','1',3,received,1), ('chat-b','1',5,0,0), ('chat-a','2',99,99,1)])
        return source
    first=snapshot('general.db', 0)
    register_sources(project,'a',tmp_path/'session.db',tmp_path/'contact.db',None,[first])
    reader=MacPaymentStateReader(project,'a')
    assert reader.states() == {('chat-b','1'):'expired'}
    second_dir=tmp_path/'new';second_dir.mkdir()
    second=snapshot('new/general.db', 2)
    before=second.read_bytes()
    register_sources(project,'a',tmp_path/'session.db',tmp_path/'contact.db',None,[second])
    assert reader.states() == {('chat-a','1'):'received',('chat-b','1'):'expired'}
    assert MacPaymentStateReader(project,'b').states() == {}
    returned=reader.states();returned.clear()
    assert reader.states() and second.read_bytes() == before
    second.unlink()
    assert reader.states() == {}


def test_mac_solitaire_snapshot_links_exact_messages_and_clears_stale_relationships(tmp_path):
    source=tmp_path/'solitaire.db'; index=tmp_path/'index.db'; suffix='a'*32
    with closing(sqlite3.connect(source)) as con, con:
        con.execute(f'CREATE TABLE Solitaire_{suffix}(header,footer,sid)')
        con.execute(f'CREATE TABLE SolitaireValid_{suffix}(title,valid,local_id,sort_seq,create_time)')
        con.execute(f'INSERT INTO Solitaire_{suffix} VALUES(?,?,?)',('activity','','native-id'))
        con.executemany(f'INSERT INTO SolitaireValid_{suffix} VALUES(?,?,?,?,?)',[
            ('activity',1,1,100,1),('activity',1,2,200,2),('activity',0,3,300,3)])
    with closing(sqlite3.connect(index)) as con, con:
        con.execute('CREATE TABLE messages(uid,account,chat_table,local_id,sort_seq,create_time,local_type)')
        con.executemany('INSERT INTO messages VALUES(?,?,?,?,?,?,?)',[
            (str(i),'account','Msg_'+suffix,i,i*100,i,(53<<32)|49) for i in (1,2,3)])
    before=source.read_bytes()
    assert refresh_solitaire(source,index,'account') == {'status':'ready','linked':2}
    links=read_memberships(index,'account')
    assert set(links)=={'1','2'} and links['1']==links['2']
    assert read_memberships(index,'other') == {} and source.read_bytes()==before
    assert refresh_solitaire(None,index,'account')['status']=='unavailable'
    assert read_memberships(index,'account')=={}


def test_mac_rich_cards_use_scoped_fields_and_keep_shared_windows_decoder_unchanged(monkeypatch):
    from noteford import repository
    from noteford.decode import decode_message as shared
    mini='<msg><appmsg><title>page</title><type>109</type><weappinfo><weappiconurl>https://wx.qlogo.cn/icon</weappiconurl></weappinfo></appmsg></msg>'
    local_type=(109<<32)|49
    assert shared(local_type,mini).kind == 'app'
    monkeypatch.setattr('sys.platform','win32')
    assert repository.decode_message(local_type,mini).kind == 'app'
    monkeypatch.setattr('sys.platform','darwin')
    result=repository.decode_message(local_type,mini)
    assert result.kind == 'mini_program' and result.avatar_url == 'https://wx.qlogo.cn/icon'
    assert result.thumbnail_url is None  # An app icon is not its page screenshot.
    channel='<msg><appmsg><title>share</title><findernamecard><nickname>unrelated</nickname></findernamecard><finderFeed><nickname>author</nickname><desc>complete video description</desc><mediaList><media><fullCoverUrl>https://wx.qpic.cn/cover</fullCoverUrl></media></mediaList></finderFeed></appmsg></msg>'
    result=decode_message((51<<32)|49,channel)
    assert (result.title,result.text,result.cover_url)==('author','complete video description','https://wx.qpic.cn/cover')


def test_mac_merged_records_expand_nested_text_without_exposing_download_secrets():
    import html
    record='''<recordinfo><datalist>
        <dataitem datatype="1"><sourcename>Alice</sourcename><sourcetime>2026-10-18</sourcetime><datadesc>first line\nsecond line</datadesc></dataitem>
        <dataitem datatype="17"><recordinfo><datalist><dataitem datatype="1"><sourcename>Bob</sourcename><datadesc>nested</datadesc></dataitem></datalist></recordinfo></dataitem>
        <dataitem datatype="2"><datatitle>picture</datatitle><cdn_dataurl>private-download-reference</cdn_dataurl><cdn_datakey>private-key</cdn_datakey></dataitem>
        </datalist></recordinfo>'''
    xml='<msg><appmsg><title>chat history</title><recorditem>'+html.escape(record)+'</recorditem></appmsg></msg>'
    result=decode_message((19<<32)|49,xml)
    assert all(value in result.text for value in ['Alice','2026-10-18','first line\nsecond line','Bob','nested','[图片] picture'])
    assert '尚未读取' not in result.text
    assert 'private' not in result.text and '<' not in result.text
    assert decode_message((19<<32)|49,'<msg><appmsg><title>chat history</title></appmsg></msg>').text=='聊天记录正文尚未同步。'


def test_mac_merged_records_reject_entities_and_bound_nesting():
    import html
    entity='<!DOCTYPE recordinfo [<!ENTITY x "private">]><recordinfo><datalist><dataitem datatype="1"><datadesc>&x;</datadesc></dataitem></datalist></recordinfo>'
    xml=lambda record:'<msg><appmsg><recorditem>'+html.escape(record)+'</recorditem></appmsg></msg>'
    assert decode_message((19<<32)|49,xml(entity)).text=='聊天记录正文尚未同步。'
    record='<recordinfo><datalist><dataitem datatype="1"><datadesc>too deep</datadesc></dataitem></datalist></recordinfo>'
    for _ in range(7):record='<recordinfo><datalist><dataitem datatype="17">'+record+'</dataitem></datalist></recordinfo>'
    result=decode_message((19<<32)|49,xml(record))
    assert '过深' in result.text and 'too deep' not in result.text


def test_mac_publication_digest_keeps_all_articles_and_their_own_links():
    xml='''<msg><appmsg><title>Daily digest</title><url>https://example.test/wrong</url>
    <mmreader><category><item><title>First</title><digest>First summary</digest><url>https://mp.weixin.qq.com/s?id=1&amp;part=one</url></item>
    <item><title>Second</title><digest>Second summary</digest><url>https://mp.weixin.qq.com/s?id=2</url></item></category></mmreader>
    </appmsg></msg>'''
    result=decode_message((5<<32)|49,xml)
    assert result.url is None and result.title=='Daily digest'
    assert all(part in result.text for part in ['1. First','First summary','id=1&part=one','2. Second','Second summary','id=2'])
    assert 'wrong' not in result.text
