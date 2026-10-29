import json
import os
import subprocess
import sys
from pathlib import Path
import pytest
import psutil
from noteford.libraries import load_root_config, registry_from_config
from noteford.runtime_lock import RuntimeLock


def test_fresh_config_is_independent_of_working_directory(tmp_path, monkeypatch):
    home = tmp_path/'new-machine'; home.mkdir()
    config = home/'config.json'
    config.write_text(json.dumps({'project_root':'app-data','library_registry_path':'app-data/data/registry.json','wechat_roots':['source'],'voice_decoder_path':'tools/decoder.exe'}))
    monkeypatch.chdir(tmp_path)
    resolved = load_root_config(config)
    assert Path(resolved['project_root']) == home/'app-data'
    assert Path(resolved['wechat_roots'][0]) == home/'source'
    assert Path(resolved['voice_decoder_path']) == home/'tools/decoder.exe'
    assert registry_from_config(config).list() == []
    assert not (tmp_path/'app-data').exists()


def test_process_lock_recovers_after_termination(tmp_path):
    code = "from pathlib import Path; from noteford.runtime_lock import RuntimeLock; import sys,time,os; lock=RuntimeLock(Path(sys.argv[1])); lock.__enter__(); print('ready:'+str(os.getpid()),flush=True); time.sleep(60)"
    child = subprocess.Popen([sys.executable,'-c',code,str(tmp_path)],stdout=subprocess.PIPE,text=True)
    writer = None
    try:
        line = child.stdout.readline().strip()
        assert line.startswith('ready:')
        writer = psutil.Process(int(line.split(':')[1]))
        with pytest.raises(RuntimeError,match='data_root_in_use'):
            with RuntimeLock(tmp_path): pass
    finally:
        # A Windows venv launcher can be a separate process from the lock owner.
        if writer is not None and writer.is_running():
            writer.kill(); writer.wait(timeout=5)
        if child.poll() is None: child.terminate()
        child.wait(timeout=5); child.stdout.close()
    with RuntimeLock(tmp_path): pass
