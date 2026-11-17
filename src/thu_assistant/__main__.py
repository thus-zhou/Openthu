"""Openthu CLI: legacy operations plus the facade-backed subcommands (6.1).

Every command prints exactly one JSON object on stdout; diagnostics never go
there.  The original operations (``services``, ``discover``, ``login``,
``read``, ``courses``, ``model``) keep their flags so existing scripts keep
working.  New subcommands route through the business facade:

``describe``   capability registration for one service
``health``     static capability health, no live probe
``query``      one registered read operation
``agenda``     local agenda read, or an explicit local calendar export
``dashboard``  start the local read-only board when its provider is present

6.2 owns ``thu_assistant.local_api``.  ``dashboard`` starts one instance, prints
the single JSON object the CLI contract promises (status, URL, port, instance)
and then serves until interrupted, so the user can actually open the board.
``--check`` starts and stops one instance and exits.  A missing provider stays
``LOCAL_DASHBOARD_UNAVAILABLE`` instead of pretending the board started.
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

from .mcp_stdio import CampusAssistant
from .bridge import CampusError, close_gateway
from . import source_state

PARAMETER_LIMIT = 65536


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='openthu', description='Openthu 清华校园服务助手')
    commands = parser.add_subparsers(dest='command', required=True)

    services = commands.add_parser('services', help='查询清华官方服务目录')
    services.add_argument('--query')
    commands.add_parser('discover', help='刷新 info 门户应用目录')
    login = commands.add_parser('login', help='完成统一认证')
    login.add_argument('--service')
    read = commands.add_parser('read', help='读取一个清华 HTTPS 页面或 JSON')
    read.add_argument('--service')
    read.add_argument('--url')
    read.add_argument('--via-webvpn', action='store_true')
    commands.add_parser('courses', help='列出网络学堂课程')
    model = commands.add_parser('model', help='调用学校模型')
    model.add_argument('--prompt')

    describe = commands.add_parser('describe', help='列出业务门面的能力登记')
    describe.add_argument('--service')
    health = commands.add_parser('health', help='报告本地能力健康状态；不探测真实站点')
    health.add_argument('--service')
    query = commands.add_parser('query', help='执行一个已登记的只读查询')
    query.add_argument('--service', required=True)
    query.add_argument('--operation', required=True)
    query.add_argument('--parameters', default='{}', help='JSON 对象，或 @文件路径')
    query.add_argument('--cursor')
    query.add_argument('--page-size', type=int, default=50)

    agenda = commands.add_parser('agenda', help='读取统一日程，或导出到本地文件')
    agenda.add_argument('--start-date')
    agenda.add_argument('--end-date')
    agenda.add_argument('--services', help='逗号分隔的来源')
    agenda.add_argument('--semester')
    agenda.add_argument('--export', help='导出到绝对路径；不导出时只打印 JSON')
    agenda.add_argument('--format', choices=['ics', 'markdown', 'json'], default='ics')
    agenda.add_argument('--overwrite', action='store_true')
    agenda.add_argument('--allow-partial', action='store_true')

    dashboard = commands.add_parser('dashboard', help='启动本地只读看板（6.2 提供）')
    dashboard.add_argument('--host', default='127.0.0.1')
    dashboard.add_argument('--port', type=int, default=0)
    dashboard.add_argument('--check', action='store_true',
                           help='启动一次、打印监听信息后立即停止，不常驻')
    return parser


def _parameters(value: str) -> dict:
    text = value if isinstance(value, str) else '{}'
    if text.startswith('@'):
        path = Path(text[1:])
        try:
            text = path.read_text(encoding='utf-8')
        except OSError:
            raise CampusError('INVALID_ARGUMENT_VALUE') from None
    if len(text.encode('utf-8')) > PARAMETER_LIMIT:
        raise CampusError('INVALID_ARGUMENT_LENGTH')
    try:
        parsed = json.loads(text or '{}')
    except ValueError:
        raise CampusError('INVALID_ARGUMENT_VALUE') from None
    if not isinstance(parsed, dict):
        raise CampusError('INVALID_ARGUMENT_TYPE')
    return parsed


def _services(value: str | None) -> list[str] | None:
    if value is None:
        return None
    rows = [item.strip() for item in value.split(',') if item.strip()]
    if not rows:
        raise CampusError('INVALID_TODO_SERVICES')
    return rows


def _run(operation: str, args, assistant: CampusAssistant):
    """Dispatch one command; returns the result object."""
    if operation == 'services':
        return assistant.call('list_services', {'query': args.query} if args.query else {})
    if operation == 'discover':
        return assistant.call('refresh_service_directory', {})
    if operation == 'login':
        return assistant.call('login', {'service_id': args.service} if args.service else {})
    if operation == 'read':
        target = {'service_id': args.service} if args.service else {'url': args.url}
        if not target.get('service_id') and not target.get('url'):
            raise CampusError('INVALID_ARGUMENT_VALUE')
        return assistant.call('read_page', {**target, 'via_webvpn': args.via_webvpn})
    if operation == 'courses':
        return assistant.call('list_courses', {})
    if operation == 'model':
        return assistant.call('model_chat', {'prompt': args.prompt or ''})
    if operation == 'describe':
        return assistant.facade.describe(args.service)
    if operation == 'health':
        return assistant.facade.health(args.service)
    if operation == 'query':
        return assistant.facade.query(service_id=args.service, operation=args.operation,
                                      parameters=_parameters(args.parameters),
                                      cursor=args.cursor, page_size=args.page_size)
    if operation == 'agenda':
        if args.export:
            return assistant.facade.export_agenda(
                args.export, start_date=args.start_date, end_date=args.end_date,
                services=_services(args.services), format=args.format,
                overwrite=args.overwrite, allow_partial=args.allow_partial)
        parameters = {}
        for key, value in (('start_date', args.start_date), ('end_date', args.end_date),
                           ('semester', args.semester)):
            if value:
                parameters[key] = value
        services = _services(args.services)
        if services:
            parameters['services'] = services
        return assistant.facade.query(service_id='local', operation='agenda',
                                      parameters=parameters)
    if operation == 'dashboard':
        return _dashboard(args, assistant)
    raise CampusError('UNKNOWN_COMMAND')


class _Emitted:
    """Marker: the command already wrote its one JSON object."""

    __slots__ = ()


_EMITTED = _Emitted()


def _dashboard(args, assistant: CampusAssistant):
    """Start the local board through its provider, or fail with the gap code."""
    if args.host not in ('127.0.0.1', '::1', 'localhost'):
        raise CampusError('INVALID_ARGUMENT_VALUE')
    if type(args.port) is not int or not 0 <= args.port <= 65535:
        raise CampusError('INVALID_ARGUMENT_RANGE')
    try:
        module = importlib.import_module('thu_assistant.local_api')
    except ImportError:
        raise CampusError('LOCAL_DASHBOARD_UNAVAILABLE') from None
    serve = getattr(module, 'serve', None)
    server_type = getattr(module, 'LocalApiServer', None)
    if not callable(serve) or not callable(server_type):
        raise CampusError('LOCAL_DASHBOARD_UNAVAILABLE')
    bind = '127.0.0.1' if args.host == 'localhost' else args.host
    if args.check:
        server = server_type(assistant.facade, bind=bind, port=args.port).start()
        try:
            return {'status': 'LISTENING', 'mode': 'CHECK', 'url': server.base_url,
                    'port': server.port, 'instance_id': server.instance_id}
        finally:
            server.stop()
    server = server_type(assistant.facade, bind=bind, port=args.port).start()
    # The one JSON line goes out before the long-running loop: a user who starts
    # the board needs the URL, and the CLI contract allows exactly one object.
    _emit({'ok': True, 'command': args.command,
           'result': {'status': 'LISTENING', 'mode': 'SERVE', 'url': server.base_url,
                      'port': server.port, 'instance_id': server.instance_id}})
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return _EMITTED


def _emit(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    args = _parser().parse_args(argv)
    assistant = None
    try:
        assistant = CampusAssistant()
        result = _run(args.command, args, assistant)
        if result is _EMITTED:
            return 0
        _emit({'ok': True, 'command': args.command, 'result': result})
        return 0
    except SystemExit:
        raise
    except Exception as error:
        raw_code = getattr(error, 'code', None)
        code = raw_code if isinstance(raw_code, str) and raw_code else type(error).__name__
        public = source_state.public_error(code if isinstance(code, str) else 'UNKNOWN')
        _emit({'ok': False, 'command': args.command, 'error': public})
        return 1
    finally:
        close_gateway()
        if assistant is not None:
            assistant.browsers.close_all()


if __name__ == '__main__':
    raise SystemExit(main())
