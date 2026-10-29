import sqlite3
from pathlib import Path
from noteford.solitaire import native_memberships, read_memberships


def test_native_membership_requires_explicit_valid_message_identity(tmp_path):
    native = tmp_path / 'native.db'
    index = tmp_path / 'index.db'
    suffix = 'a' * 32
    with sqlite3.connect(native) as c:
        c.execute(f'CREATE TABLE Solitaire_{suffix}(header,footer,sid)')
        c.execute(f'CREATE TABLE SolitaireValid_{suffix}(title,valid,local_id,sort_seq,create_time)')
        c.execute(f'INSERT INTO Solitaire_{suffix} VALUES (?,?,?)', ('活动', '', ''))
        c.executemany(f'INSERT INTO SolitaireValid_{suffix} VALUES (?,?,?,?,?)', [
            ('活动',1,1,1000,1), ('活动',1,2,2000,2), ('活动',0,3,3000,3),
            ('未知',1,4,4000,4), ('活动',1,5,5000,5)])
    with sqlite3.connect(index) as c:
        c.execute('CREATE TABLE messages(uid,account,chat_table,local_id,sort_seq,create_time,local_type)')
        c.executemany('INSERT INTO messages VALUES (?,?,?,?,?,?,?)', [
            (str(i), 'account', 'Msg_'+suffix, i, i*1000, i, (53<<32)|49) for i in range(1,6)])
        c.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?)', ('collision','account','Msg_'+suffix,5,5000,5,(53<<32)|49))
        c.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?)', ('other-group','account','Msg_'+'b'*32,1,1000,1,(53<<32)|49))
    links = native_memberships(native,index,'account')
    assert set(links) == {'1','2'}
    assert links['1'] == links['2']
    assert native_memberships(native,index,'other-account') == {}
    # The native validity table cannot disambiguate equal headers with different footers.
    with sqlite3.connect(native) as c:
        c.execute(f'INSERT INTO Solitaire_{suffix} VALUES (?,?,?)', ('活动','另一活动',''))
    assert native_memberships(native,index,'account') == {}


def test_membership_file_is_account_scoped(tmp_path):
    (tmp_path/'solitaire-memberships.json').write_text('{"account":"one","links":{"u":"key"}}')
    assert read_memberships(tmp_path/'index.db','one') == {'u':'key'}
    assert read_memberships(tmp_path/'index.db','two') == {}
