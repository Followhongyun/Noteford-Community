from types import SimpleNamespace
import pytest
from noteford.download_policy import automatic, check_size
from noteford.cdn_download import DownloadError
from test_sticker_parsing import settings_api


def test_settings_are_durable_scoped_and_invalid_save_is_atomic(tmp_path, monkeypatch):
    a,b=settings_api(tmp_path/'a'),settings_api(tmp_path/'b')
    core=SimpleNamespace(config={});a._cores['current']=core
    for limit in (20,30,50,0):
        assert a.update_auto_media_limit(limit)['auto_media_max_mb']==limit
        assert settings_api(tmp_path/'a').settings()['auto_media_max_mb']==limit
        assert core.config['auto_media_max_mb']==limit
    a.update_auto_media_limit(30)
    assert b.settings()['auto_media_max_mb']==0
    for invalid in (True,None,21,'20',-1):
        with pytest.raises(ValueError):a.update_auto_media_limit(invalid)
    monkeypatch.setattr('noteford.service.os.replace',lambda *a: (_ for _ in ()).throw(OSError()))
    with pytest.raises(OSError):a.update_auto_media_limit(50)
    assert core.config['auto_media_max_mb']==30
    assert settings_api(tmp_path/'a').settings()['auto_media_max_mb']==30


@pytest.mark.parametrize('limit',[20,30,50])
def test_size_boundaries_and_manual_isolation(limit):
    with automatic({'auto_media_max_mb':limit}):
        check_size(limit*1024*1024)
        check_size(limit*1024*1024+16,encrypted=True)
        with pytest.raises(DownloadError,match='auto_size_limit'):
            check_size(limit*1024*1024+1)
    check_size(100*1024*1024)
    with automatic({'auto_media_max_mb':0}):check_size(100*1024*1024)
