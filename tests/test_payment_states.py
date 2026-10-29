import sqlite3

from noteford.payment_states import PaymentStateReader, read_states, terminal_state


def test_terminal_states_and_unknowns():
    assert terminal_state(3, 2, 1) == 'received'
    assert terminal_state(4, 0, 1) == 'exhausted'
    assert terminal_state(5, 0, 0) == 'expired'
    for codes in ((1, 0, 0), (3, 0, 1), (2, 1, 1), (99, 99, 1), (4, 0, 99)):
        assert terminal_state(*codes) is None


def test_exact_session_and_message_join_and_conflicts(tmp_path):
    database = tmp_path / 'states.db'
    con = sqlite3.connect(database)
    con.execute('create table redEnvelopeTable(session_name,message_server_id,hb_status,receive_status,hb_type)')
    con.executemany('insert into redEnvelopeTable values(?,?,?,?,?)', [
        ('chat-a', '123', 3, 2, 1), ('chat-b', '123', 3, 0, 1),
        ('chat-a', '456', 4, 0, 1), ('chat-a', '789', 3, 2, 1),
        ('chat-a', '789', 3, 0, 1), ('chat-a', '0', 4, 0, 1),
    ])
    con.commit()
    con.close()
    assert read_states(database) == {('chat-a', '123'): 'received', ('chat-a', '456'): 'exhausted'}


def test_missing_source_and_failed_snapshot_clear_cached_state(tmp_path, monkeypatch):
    source = tmp_path / 'source.db'
    reader = PaymentStateReader(source, tmp_path / 'runtime', tmp_path / 'missing-keys')
    reader._states = {('chat', '123'): 'received'}
    assert reader.states() == {}
    source.write_bytes(b'invalid database')
    reader._checked = float('-inf')
    assert reader.states() == {}
    assert not list((tmp_path / 'runtime').glob('payment-state-*'))
    assert source.read_bytes() == b'invalid database'
