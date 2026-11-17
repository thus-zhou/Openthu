"""Generate a machine-local MCP configuration without reading credentials."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import sys

ROOT=Path(__file__).resolve().parents[1]
_SRC=ROOT/'src'
if str(_SRC) not in sys.path:sys.path.insert(0,str(_SRC))

from thu_assistant import paths as path_resolver


def configuration():
    """Describe this machine's install in the frozen resource vocabulary.

    The helper and the pinned upstream proxy are resolved through the same
    logical ids the runtime uses, so a source checkout and a complete wheel
    both produce a configuration that points at files which actually exist.
    """
    helper=path_resolver.resolve_resource('source.windows_auth')
    vendor=path_resolver.resolve_resource('source.madmodel_proxy')
    missing=[row.id for row in (helper,vendor) if not row.available]
    if missing:
        raise SystemExit('缺少运行资源：'+'、'.join(missing)+'；请使用完整源码包或完整安装包')
    return {'mcpServers':{'tsinghua-assistant':{
        'command':sys.executable,'args':['-m','thu_assistant.mcp_stdio'],
        'env':{'PYTHONUTF8':'1','PYTHONPATH':str(ROOT/'src'),
               'THU_PYTHON':sys.executable,'THU_NODE':shutil.which('node') or 'node',
               'THU_MADMODEL_DIR':os.environ.get('THU_MADMODEL_DIR') or str(vendor.path.parent),
               'THU_ACCESS_HELPER':os.environ.get('THU_ACCESS_HELPER') or str(helper.path),
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
