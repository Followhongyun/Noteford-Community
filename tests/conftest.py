"""Never let storage regression tests touch the developer's login Keychain."""
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_mac_keychain(tmp_path, monkeypatch):
    from noteford import keychain_secret
    items = {}
    monkeypatch.setattr(keychain_secret, 'get_blob', lambda service, account: items.get((service, account)))
    monkeypatch.setattr(keychain_secret, 'put_blob', lambda service, account, payload: items.__setitem__((service, account), payload))
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path / 'home'))
    return items
