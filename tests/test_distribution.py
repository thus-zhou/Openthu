"""Public configuration, privacy scanning and manual-checkpoint contracts."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]


def load(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/f'{name}.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_scanner_flags_synthetic_secret_without_echoing_its_value():
    scanner=load('privacy_scan')
    value='ghp_'+'a'*36
    rows=scanner.findings('example.txt',value.encode())
    assert rows==[{'file':'example.txt','line':1,'rule':'github-token'}]
    assert value not in json.dumps(rows)


def test_configuration_generated_for_current_checkout_without_credentials(tmp_path):
    output=tmp_path/'mcp.json'
    subprocess.run([sys.executable,str(ROOT/'scripts/configure_mcp.py'),'--output',str(output)],check=True,capture_output=True)
    server=json.loads(output.read_text(encoding='utf-8'))['mcpServers']['tsinghua-assistant']
    assert server['command']==sys.executable
    assert server['env']['PYTHONPATH']==str(ROOT/'src')
    assert set(server['env'])=={'PYTHONUTF8','PYTHONPATH','THU_PYTHON','THU_NODE','THU_MADMODEL_DIR','THU_ACCESS_HELPER','THU_LEARN_PROVIDER'}


def new_relay_repo(tmp_path):
    (tmp_path/'scripts').mkdir();(tmp_path/'handoffs').mkdir()
    shutil.copyfile(ROOT/'scripts/handoff.py',tmp_path/'scripts/handoff.py')
    (tmp_path/'handoffs/TEMPLATE.md').write_text('unfilled template\n')
    (tmp_path/'handoffs/T1.md').write_text('Goal: synthetic task; reproduce using fixtures; actual: failing fixture; next: Codex\n')
    def git(*args):
        return subprocess.run(['git',*args],cwd=tmp_path,check=True,capture_output=True,text=True).stdout.strip()
    git('init','-b','hermes/1-example');git('config','user.name','Synthetic Test')
    git('config','user.email','tests@example.invalid');git('config','commit.gpgsign','false')
    git('add','.');git('commit','-m','Synthetic task state')
    return git


def test_manual_handoff_pins_committed_code_and_has_no_dispatch(tmp_path):
    git=new_relay_repo(tmp_path)
    commit=git('rev-parse','HEAD')
    result=subprocess.run([sys.executable,str(tmp_path/'scripts/handoff.py'),'T1','--to','codex'],cwd=tmp_path,capture_output=True,text=True)
    assert result.returncode==0
    checkpoint=json.loads((tmp_path/'handoffs/T1.json').read_text())
    assert checkpoint['code_commit']==commit
    assert checkpoint['next_owner']=='codex' and checkpoint['dispatch']=='manual'
    assert git('rev-parse','HEAD')==commit


def test_manual_handoff_refuses_to_lose_dirty_work(tmp_path):
    new_relay_repo(tmp_path)
    (tmp_path/'unfinished.txt').write_text('not committed\n')
    result=subprocess.run([sys.executable,str(tmp_path/'scripts/handoff.py'),'T1','--to','codex'],cwd=tmp_path,capture_output=True,text=True)
    assert result.returncode!=0
    assert not (tmp_path/'handoffs/T1.json').exists()
