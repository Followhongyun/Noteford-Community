import json
from pathlib import Path
import os
import psutil
import pytest
from noteford.workspace_access import WorkspaceAccess, require_source

def lease(root):
    root.mkdir()
    (root/'.community-workspace.json').write_text(json.dumps({'version':1,'installation':str(root),'workspace':'community'}))
    f=root/'data/runtime/community/active-workspace.json';f.parent.mkdir(parents=True)
    value={'version':1,'active':True,'root':str(root),'workspace':'community','epoch':'one',
           'pid':os.getpid(),'appExecutable':psutil.Process().exe(),'processStartedAtMs':psutil.Process().create_time()*1000}
    f.write_text(json.dumps(value))
    return f,value

def test_local_reader_rejects_closed_or_replaced_desktop(tmp_path):
    root=tmp_path/'local';f,value=lease(root)
    reader=WorkspaceAccess(root);reader.check()
    value['epoch']='two';f.write_text(json.dumps(value))
    with pytest.raises(PermissionError):reader.check()
    value['epoch']='one';value['active']=False;f.write_text(json.dumps(value))
    with pytest.raises(PermissionError):reader.check()

def test_foreign_managed_archive_cannot_be_imported(tmp_path):
    root=tmp_path/'local';lease(root)
    other=tmp_path/'other';lease(other)
    with pytest.raises(PermissionError):require_source(root,other/'data')
    with pytest.raises(PermissionError):require_source(root,tmp_path)
