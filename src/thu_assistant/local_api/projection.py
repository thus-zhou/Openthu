"""Display projection for the local read API.

The dashboard receives a *projected* copy of a facade envelope: credential-like
keys are dropped, private paths and token shapes are redacted, links are limited
to verified school HTTPS sources, and every response carries a small ``display``
block with a completeness summary and one next step.  Projection never changes
the business status: a failed read stays failed.
"""
from __future__ import annotations

from datetime import datetime
import math
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

from .. import source_state
from . import contract

MAX_TEXT = 2000
MAX_DEPTH = 8
LINK_KEYS = frozenset({
    'url', 'href', 'link', 'links', 'source_url', 'download_url', 'entry_url',
    'landing_url', 'official_url', 'portal_url',
})
SECRET_PATTERNS = (
    re.compile(r'\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b'),
    re.compile(r'\bBearer\s+[A-Za-z0-9._~+/=-]{8,}'),
    re.compile(r'\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{16,}\b'),
    re.compile(r'\bgh[pousr]_[A-Za-z0-9]{20,}\b'),
    re.compile(r'(?i)\b(?:set-)?cookie\s*[:=]\s*\S+'),
    re.compile(r'(?i)\bticket\s*[:=]\s*[A-Za-z0-9._~+/=-]{8,}'),
)
PATH_PATTERNS = (
    re.compile(r'(?<![\w.-])/(?:home|Users|root|tmp|mnt|var/folders|private/var)/[^\s"\']*'),
    re.compile(r'(?i)\b[A-Za-z]:\\[^\s"\']*'),
    re.compile(r'\\\\[A-Za-z0-9._-]+\\[^\s"\']*'),
)
AUTH_PATH = re.compile(r'(?i)(?:/cas(?:/|$)|/sso(?:/|$)|/login(?:/|$)|/auth(?:/|$)'
                       r'|/oauth(?:/|$)|/idp(?:/|$)|service=|ticket=|client_id=)')
ALLOWED_LINK_SUFFIX = 'tsinghua.edu.cn'

STATUS_DISPLAY = {
    'OK': ('READY', '结果已按当前范围完整读取。', '无'),
    'EMPTY': ('EMPTY', '没有符合条件的记录。', '调整筛选条件或稍后再看。'),
    'PARTIAL': ('PARTIAL', '来源未完整读取，结果只覆盖已读取范围。', '稍后重试或缩小范围。'),
    'AUTH_REQUIRED': ('LOGIN_REQUIRED', '需要先在受控入口完成校园认证。',
                      '在原有受控入口完成官方认证后再刷新；本机看板不代登录。'),
    'AUTHENTICATED': ('READY', '已认证页面读取成功。', '无'),
    'PUBLIC_READABLE': ('READY', '公开页面读取成功。', '无'),
    'READABLE_AUTH_UNKNOWN': ('READY', '页面可读，但登录状态未验证。', '需要登录后才能办理的操作请使用受控入口。'),
    'FORBIDDEN': ('FORBIDDEN', '当前账号没有该权限。', '确认账号或使用受控入口。'),
    'RATE_LIMITED': ('LIMITED', '学校来源要求稍后重试。', '稍后重试。'),
    'MAINTENANCE': ('MAINTENANCE', '学校服务正在维护。', '稍后重试。'),
    'TIMEOUT': ('LIMITED', '读取超时。', '稍后重试；其他卡片不受影响。'),
    'CANCELLED': ('LIMITED', '本次读取已取消。', '需要时重新发起。'),
    'INTERRUPTED': ('LIMITED', '读取被中断。', '稍后重试。'),
    'UNKNOWN': ('UNKNOWN', '当前状态未知。', '稍后重试；未知不等于成功。'),
}

CODE_DISPLAY = {
    'LOCAL_API_SESSION_REQUIRED': ('LOCAL_SESSION_REQUIRED', '本地看板会话不存在。',
                                   '重新打开本地看板建立新的本地会话；不需要重新登录学校。'),
    'LOCAL_API_SESSION_EXPIRED': ('LOCAL_SESSION_REQUIRED', '本地看板会话已过期。',
                                  '重新打开本地看板建立新的本地会话；不需要重新登录学校。'),
    'LOCAL_API_SESSION_INVALID': ('LOCAL_SESSION_REQUIRED', '本地看板会话无效。',
                                  '重新打开本地看板建立新的本地会话；不需要重新登录学校。'),
    'LOCAL_API_RATE_LIMITED': ('LIMITED', '请求过于频繁。', '稍后重试。'),
    'LOCAL_API_REFRESH_TOO_SOON': ('LIMITED', '刷新过于频繁。', '等待提示的时间后再刷新。'),
    'LOCAL_API_READ_TIMEOUT': ('LIMITED', '读取超时。', '稍后重试；其他卡片不受影响。'),
    'LOCAL_API_FACADE_UNAVAILABLE': ('UNKNOWN', '业务门面不可用。',
                                     '本机看板尚未接入业务门面；请使用原有工具入口。'),
    'LOCAL_API_FACADE_INVALID_RESPONSE': ('UNKNOWN', '业务门面返回了无法识别的结果。',
                                          '这是内部契约问题，请记录请求编号并稍后重试。'),
    'LOCAL_API_UI_NOT_INSTALLED': ('UNKNOWN', '本地界面资源尚未安装。',
                                   '本地读取 API 可用；界面资源由后续模块安装。'),
    'LOCAL_API_HOST_NOT_ALLOWED': ('FORBIDDEN', '拒绝未知的主机名。', '通过本机看板入口重新打开。'),
    'LOCAL_API_ORIGIN_NOT_ALLOWED': ('FORBIDDEN', '拒绝了外部来源的请求。', '通过本机看板入口重新打开。'),
}


def _clear(value: str, warnings: set[str]) -> str:
    text = ''.join(ch for ch in value if ch in '\n\t' or ord(ch) >= 0x20)
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT] + '…'
        warnings.add('LOCAL_API_TEXT_TRUNCATED')
    for pattern in SECRET_PATTERNS:
        replaced = pattern.sub('[REDACTED]', text)
        if replaced != text:
            warnings.add('LOCAL_API_CREDENTIAL_REDACTED')
        text = replaced
    for pattern in PATH_PATTERNS:
        replaced = pattern.sub('[REDACTED]', text)
        if replaced != text:
            warnings.add('LOCAL_API_ABSOLUTE_PATH_REDACTED')
        text = replaced
    return text


def safe_link(value: Any) -> tuple[str | None, str | None]:
    """Return ``(url, warning)``; only a verified school HTTPS link survives."""
    if not isinstance(value, str) or not value.strip():
        return None, 'LOCAL_API_LINK_WITHHELD'
    try:
        parsed = urlsplit(value.strip())
        host = (parsed.hostname or '').lower()
    except ValueError:
        return None, 'LOCAL_API_LINK_WITHHELD'
    if parsed.scheme != 'https' or not host:
        return None, 'LOCAL_API_LINK_WITHHELD'
    if host != ALLOWED_LINK_SUFFIX and not host.endswith('.' + ALLOWED_LINK_SUFFIX):
        return None, 'LOCAL_API_LINK_WITHHELD'
    if AUTH_PATH.search(value) or AUTH_PATH.search(parsed.path) or AUTH_PATH.search(parsed.query):
        return None, 'LOCAL_API_LINK_WITHHELD'
    sanitized = source_state.sanitize_url(value)
    if not sanitized:
        return None, 'LOCAL_API_LINK_WITHHELD'
    return sanitized, None


def _walk(value: Any, *, depth: int, warnings: set[str], key: str | None = None) -> Any:
    if depth > MAX_DEPTH:
        warnings.add('LOCAL_API_DEPTH_LIMIT')
        return None
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            name = str(raw_key)
            folded = name.casefold()
            if folded in contract.DENY_KEYS:
                warnings.add('LOCAL_API_SENSITIVE_FIELD_DROPPED')
                continue
            if folded in ('links', 'link') and isinstance(item, Mapping):
                links: dict[str, Any] = {}
                for label, target in item.items():
                    if isinstance(target, str):
                        url, warning = safe_link(target)
                        if warning:
                            warnings.add(warning)
                        if url:
                            links[str(label)] = url
                    elif isinstance(target, Mapping):
                        links[str(label)] = _walk(target, depth=depth + 1,
                                                  warnings=warnings, key=str(label))
                result[name] = links
                continue
            if folded in LINK_KEYS or folded.endswith(('_url', '_link', '_href')):
                if item is None:
                    result[name] = None
                elif isinstance(item, str):
                    url, warning = safe_link(item)
                    if warning:
                        warnings.add(warning)
                    result[name] = url
                else:
                    result[name] = _walk(item, depth=depth + 1, warnings=warnings, key=name)
                continue
            result[name] = _walk(item, depth=depth + 1, warnings=warnings, key=name)
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > contract.LIMITS['max_page_items']:
            raise contract.LocalApiError('LOCAL_API_RESPONSE_TOO_LARGE', http_status=502,
                                         message='结果超出本地显示上限，请缩小范围。')
        return [_walk(item, depth=depth + 1, warnings=warnings) for item in value]
    if isinstance(value, str):
        return _clear(value, warnings)
    if isinstance(value, float) and not math.isfinite(value):
        warnings.add('LOCAL_API_NON_FINITE_NUMBER')
        return None
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    warnings.add('LOCAL_API_UNSUPPORTED_VALUE')
    return None


def _status_text(status: str) -> tuple[str, str, str]:
    if status in STATUS_DISPLAY:
        return STATUS_DISPLAY[status]
    return ('UNKNOWN', '当前状态未知。', '稍后重试；未知不等于成功。')


def build_display(envelope: Mapping[str, Any], *, service_id: str,
                  operation: str, error_code: str | None = None) -> dict[str, Any]:
    status = envelope.get('status') if isinstance(envelope.get('status'), str) else 'UNKNOWN'
    if error_code and error_code in CODE_DISPLAY:
        state, message, next_step = CODE_DISPLAY[error_code]
    else:
        state, message, next_step = _status_text(status)
        if state == 'READY' and envelope.get('complete') is not True:
            state = 'PARTIAL'
            message = '来源已读取，但业务结果范围尚未确认完整。'
            next_step = '稍后重试或缩小范围；未知与不完整都不等于成功。'
        if error_code and status in source_state.ERROR_OBJECT_STATUSES:
            state = 'UNKNOWN' if state == 'ERROR' else state
    source = envelope.get('source') if isinstance(envelope.get('source'), Mapping) else {}
    url = source.get('url') if isinstance(source.get('url'), str) else None
    safe_url, _warning = safe_link(url) if url else (None, None)
    checked = envelope.get('checked_at')
    try:
        datetime.fromisoformat(str(checked).replace('Z', '+00:00'))
    except ValueError:
        checked = None
    returned = envelope.get('returned_items')
    if type(returned) is not int:
        items = envelope.get('items')
        returned = len(items) if isinstance(items, list) else None
    total = envelope.get('total_items')
    return {
        'state': state,
        'message': message,
        'next_step': next_step,
        'completeness': {
            'source_complete': envelope.get('source_complete') is True,
            'snapshot_complete': envelope.get('snapshot_complete') is True,
            'complete': envelope.get('complete') is True,
        },
        'provenance': {
            'service_id': source.get('service_id') or service_id,
            'operation': source.get('operation') or operation,
            'adapter_version': source.get('adapter_version'),
            'checked_at': checked,
            'verification_level': envelope.get('verification_level'),
            'url': safe_url,
        },
        'paging': {
            'returned_items': returned if type(returned) is int else None,
            'total_items': total if type(total) is int else None,
            'has_more': bool(envelope.get('next_cursor')),
        },
    }


def project(envelope: Mapping[str, Any], *, service_id: str, operation: str,
            request_id: str, error_code: str | None = None) -> dict[str, Any]:
    """Return the display-safe response body for one facade envelope."""
    warnings: set[str] = set()
    projected = _walk(envelope, depth=0, warnings=warnings)
    projected = dict(projected) if isinstance(projected, Mapping) else {}
    items = projected.get('items')
    if isinstance(items, list) and isinstance(projected.get('data'), list) \
            and projected.get('data') == items:
        projected.pop('data', None)
    merged = [str(item) for item in (projected.get('warnings') or [])
              if isinstance(item, str) and 0 < len(item) <= 200]
    merged.extend(sorted(warnings))
    projected['warnings'] = sorted(dict.fromkeys(merged))
    projected['request_id'] = request_id
    projected['display'] = build_display(projected, service_id=service_id,
                                         operation=operation, error_code=error_code)
    return projected


def error_body(code: str, *, request_id: str, http_status: int = 400,
               message: str | None = None, retry_after_ms: int | None = None,
               service_id: str = 'local', operation: str = 'local_api') -> dict[str, Any]:
    """A result-envelope-shaped error body with no implementation detail."""
    category = contract.category_for(code)
    error = source_state.public_error(code, category=category, message=message,
                                      retry_after_ms=retry_after_ms)
    status = contract.envelope_status_for_http(http_status)
    return {
        'request_id': request_id,
        'status': status,
        'source': {'service_id': service_id, 'operation': operation,
                   'adapter_version': contract.CONTRACT_VERSION, 'url': None},
        'checked_at': source_state.now_iso(),
        'verification_level': 'NONE',
        'source_complete': False,
        'snapshot_complete': True,
        'complete': False,
        'next_cursor': None,
        'warnings': [],
        'items': [],
        'error': error,
        'display': build_display({'status': status, 'source': {'service_id': service_id,
                                                              'operation': operation,
                                                              'adapter_version': contract.CONTRACT_VERSION},
                                  'checked_at': source_state.now_iso(),
                                  'verification_level': 'NONE', 'source_complete': False,
                                  'snapshot_complete': True, 'complete': False,
                                  'next_cursor': None, 'warnings': [], 'items': []},
                                 service_id=service_id, operation=operation,
                                 error_code=code),
    }
