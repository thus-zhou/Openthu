"""Generate a machine-local MCP configuration without reading credentials."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import sys

ROOT=Path(__file__).resolve().parents[1]


def configuration():
    return {'mcpServers':{'tsinghua-assistant':{
        'command':sys.executable,'args':['-m','thu_assistant.mcp_stdio'],
        'env':{'PYTHONUTF8':'1','PYTHONPATH':str(ROOT/'src'),
               'THU_PYTHON':sys.executable,'THU_NODE':shutil.which('node') or 'node',
               'THU_MADMODEL_DIR':os.environ.get('THU_MADMODEL_DIR') or str(ROOT/'vendor/madmodel-proxy'),
               'THU_ACCESS_HELPER':str(ROOT/'runtime/windows/access.js'),
               'THU_LEARN_PROVIDER':str(ROOT/'src')}}}}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    group=p.add_mutually_exclusive_group()
    group.add_argument('--output',type=Path,default=None)
    group.add_argument('--plugin-output',type=Path)
    args=p.parse_args()
    if args.plugin_output:
        target=args.plugin_output.resolve()
        if target==ROOT/'integrations/codex' or target.exists():
            p.error('Choose a new local destination')
        shutil.copytree(ROOT/'integrations/codex',target)
        output=target/'.mcp.json'
    else:
        output=(args.output or ROOT/'.local/mcp.json').resolve()
        if output in (ROOT/'configs/mcp.template.json',ROOT/'integrations/codex/.mcp.json'):
            p.error('Do not overwrite public templates')
        output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(configuration(),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'ok':True,'machine_local_configuration_created':True,'credentials_read':False}))


if __name__=='__main__':main()
