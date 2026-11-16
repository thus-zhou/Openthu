"""Portable, isolated development commands; no live campus/model requests."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def diagnostics_module():
    script_dir = str(Path(__file__).resolve().parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    import dev_diagnostics
    return dev_diagnostics


def configuration_module():
    src_dir = str(ROOT / 'src')
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    import thu_assistant.configuration
    return thu_assistant.configuration


def environment(state: Path, config=None) -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if key.startswith(('THU_', 'MADMODEL_', 'LEARN_ASSISTANT_', 'OPENTHU_')):
            env.pop(key)
    if os.name == 'nt' and not env.get('PLAYWRIGHT_BROWSERS_PATH'):
        base = os.environ.get('LOCALAPPDATA')
        if base:
            env['PLAYWRIGHT_BROWSERS_PATH'] = str(Path(base) / 'ms-playwright')
    configured_node = 'node'
    configured_python = sys.executable
    if config is not None:
        configured_node = str(config.runtime.node)
        configured_python = str(config.runtime.python)
    node = (
        configured_node
        if Path(configured_node).is_absolute()
        else (shutil.which(configured_node) or configured_node)
    )
    env.update({
        'PYTHONPATH': str(ROOT / 'src'), 'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1',
        'THU_NODE': node, 'THU_PYTHON': configured_python,
        'THU_MADMODEL_DIR': str(ROOT / 'vendor/madmodel-proxy'),
        'THU_ACCESS_HELPER': str(ROOT / 'runtime/windows/access.js'),
        'THU_LEARN_PROVIDER': str(ROOT / 'src'),
        'LOCALAPPDATA': str(state / 'local'), 'THU_ASSISTANT_STATE_DIR': str(state / 'campus'),
        'THU_ACCESS_STATE_DIR': str(state / 'access'), 'MADMODEL_STATE_DIR': str(state / 'madmodel'),
        'MADMODEL_CREDS_FILE': str(state / 'madmodel/creds.json'),
        'OPENTHU_DOWNLOAD_DIR': str(state / 'downloads'), 'OPENTHU_DATA_DIR': str(state / 'data'),
    })
    return env


def run(argv: list[str], **kwargs):
    return subprocess.run(argv, cwd=ROOT, text=True, encoding='utf-8', errors='replace',
                          timeout=kwargs.pop('timeout', 300), **kwargs)


def run_checked(argv: list[str], *, label: str, env: dict[str, str], **kwargs):
    """Run one configured command and convert a spawn failure to a stable error.

    A user can point ``runtime.python``/``runtime.node`` at a missing or
    non-executable path; that must not surface as a raw traceback.
    """
    try:
        return run(argv, env=env, **kwargs)
    except OSError:
        print(f'{label} 无法启动；请检查对应 runtime 配置或重新安装', file=sys.stderr)
        raise SystemExit(1) from None


def smoke(env: dict[str, str]):
    messages = [
        {'jsonrpc':'2.0','id':1,'method':'initialize','params':{}},
        {'jsonrpc':'2.0','method':'notifications/initialized'},
        {'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}},
        {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'list_services','arguments':{}}},
    ]
    result = run_checked([env.get('THU_PYTHON', sys.executable), '-m', 'thu_assistant.mcp_stdio'],
                         label='MCP 进程（runtime.python）', env=env,
                         input=''.join(json.dumps(x)+'\n' for x in messages),
                         capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError('MCP process failed; inspect local installation')
    rows={x['id']:x for line in result.stdout.splitlines() if line.strip() for x in [json.loads(line)]}
    if set(rows)!={1,2,3} or any('error' in x for x in rows.values()):
        raise RuntimeError('MCP protocol check failed')
    if rows[3]['result'].get('isError'):
        raise RuntimeError('Static catalog read failed')
    tools=rows[2]['result']['tools']
    if len(tools)!=29:
        raise RuntimeError('Unexpected tool inventory; update contract deliberately')
    return {'ok':True,'tool_count':len(tools),'live_campus_calls':0,'model_calls':0,'business_submissions':0}


def main():
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):
            stream.reconfigure(encoding='utf-8',errors='replace')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['setup-node','doctor','check','smoke','test'])
    parser.add_argument('--python-only',action='store_true')
    parser.add_argument('--node-only',action='store_true')
    parser.add_argument('--json',action='store_true',help='machine-readable diagnostics for doctor')
    parser.add_argument('--config',metavar='PATH',help='explicit configuration file')
    args=parser.parse_args()
    if args.command=='doctor':
        module=diagnostics_module()
        report=module.collect(config_file=args.config)
        if args.json:
            print(json.dumps(report,ensure_ascii=False,indent=2))
        else:
            print(module.format_report(report))
        return 0 if report['ok'] else 1
    if args.command=='setup-node':
        config_module=configuration_module()
        try:
            config=config_module.load_config(config_file=args.config)
        except config_module.ConfigurationError as error:
            print(f'配置无效：{error.item}（{error.code}）',file=sys.stderr)
            return 1
        npm=shutil.which('npm.cmd' if os.name=='nt' else 'npm')
        if not npm:
            parser.error('Node.js 24 and npm are required; install Node.js 24 from https://nodejs.org/ and retry')
        registry=config.network.npm_registry
        argv=diagnostics_module().executable_argv(
            npm,
            ['ci','--ignore-scripts','--no-audit','--no-fund','--registry='+registry],
            platform_name=os.name,
        )
        try:
            return subprocess.call(argv,cwd=ROOT/'src/thu_assistant')
        except OSError:
            print('npm 无法启动；请检查 Node.js 24 安装',file=sys.stderr)
            return 1
    config=None
    if args.command in ('check','smoke','test'):
        config_module=configuration_module()
        try:
            config=config_module.load_config(config_file=args.config)
        except config_module.ConfigurationError as error:
            print(f'配置无效：{error.item}（{error.code}）',file=sys.stderr)
            return 1
    with tempfile.TemporaryDirectory(prefix='openthu-offline-') as tmp:
        env=environment(Path(tmp),config)
        if args.command in ('check','smoke'):
            if args.command=='check':
                module=diagnostics_module()
                report=module.collect(config_file=args.config,config=config)
                if not report['ok']:
                    print(module.format_report(report),file=sys.stderr)
                    return 1
                node=run_checked([env['THU_NODE'],'--version'],
                                 label='Node 命令（runtime.node）',env=env,
                                 capture_output=True)
                node_version=module._safe_version(node.stdout)
                if node.returncode or not node_version or int(node_version.split('.')[0])<24:
                    raise RuntimeError('Node.js 24+ is required for this validated development setup')
                for p in (ROOT/'src').rglob('*.py'):
                    compile(p.read_text(encoding='utf-8'),p.name,'exec')
                scan=run_checked([env.get('THU_PYTHON',sys.executable),'scripts/privacy_scan.py'],
                                 label='隐私扫描进程（runtime.python）',env=env)
                if scan.returncode:
                    return scan.returncode
            try:
                print(json.dumps(smoke(env)))
            except RuntimeError as error:
                print(f'{args.command} failed: {error}',file=sys.stderr)
                return 1
            return 0
        results={}
        if not args.node_only:
            result=run_checked([env.get('THU_PYTHON',sys.executable),'-m','pytest','-o','addopts=','-q','-p','no:cacheprovider'],
                               label='pytest（runtime.python）',env=env,capture_output=True,timeout=600)
            print(result.stdout,end=''); print(result.stderr,end='',file=sys.stderr)
            results['python_exit']=result.returncode
            for label in ('passed','skipped','failed','errors'):
                match=re.search(r'(\d+) '+label,result.stdout)
                results['python_'+label]=int(match[1]) if match else 0
        if not args.python_only:
            files=[str(p) for p in sorted((ROOT/'tests').glob('campus-*.test.js'))]
            result=run_checked([env['THU_NODE'],'--test',*files],
                               label='Node 测试（runtime.node）',env=env,capture_output=True)
            print(result.stdout,end=''); print(result.stderr,end='',file=sys.stderr)
            results['node_exit']=result.returncode
            for label in ('pass','fail','skipped'):
                match=re.search(r'(?:# )?'+label+r'\s+(\d+)',result.stdout)
                results['node_'+label]=int(match[1]) if match else 0
        results['ok']=not any(value for name,value in results.items() if name.endswith('_exit'))
        results.update(live_campus_calls=0,model_calls=0,business_submissions=0)
        output=ROOT/'.local/test-results.json'
        output.parent.mkdir(exist_ok=True)
        output.write_text(json.dumps(results,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(results))
        return 0 if results['ok'] else 1


if __name__=='__main__':
    raise SystemExit(main())
