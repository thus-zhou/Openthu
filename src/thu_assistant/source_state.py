"""Canonical source states, completeness and public error translation.

This module owns the rules that turn an adapter observation into the frozen
v0.2 result envelope and source-evidence shapes.  It is deliberately free of
network, filesystem or credential access so the same rules run for synthetic
fixtures and live adapters.

Frozen inputs consumed here:
  * ``contracts/v0.2/result-envelope.schema.json`` (result-envelope-v1)
  * ``contracts/v0.2/source-evidence.schema.json`` (source-evidence-v1)
  * ``contracts/v0.2/error-classification.json`` (error-classification-v1)

The contract files are not package data, so the error-code mirror below is the
runtime copy.  ``tests/test_query_contracts.py`` compares it with the contract
file when the checkout is available; a drift fails the test instead of being
discovered by a caller.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

VERSION = 'source-state-v1'
TIMEZONE_NAME = 'Asia/Shanghai'
# China has used a fixed UTC+8 offset without DST since 1991.  A fixed offset
# avoids depending on the platform tz database for a display/default value.
TZ = timezone(timedelta(hours=8))

SERVICE_DOMAINS = {
    'info': 'INFO',
    'learn': 'LEARN',
    'library': 'LIBRARY',
    'sports': 'SPORTS',
    'madmodel': 'MADMODEL',
    'local': 'LOCAL',
    'campus': 'UNKNOWN',
}

# result-envelope-v1 status values.
ENVELOPE_STATUSES = {
    'OK', 'EMPTY', 'PARTIAL', 'AUTH_REQUIRED', 'FORBIDDEN', 'RATE_LIMITED',
    'MAINTENANCE', 'HTTP_ERROR', 'BROWSER_REQUIRED', 'READABLE_AUTH_UNKNOWN',
    'PUBLIC_READABLE', 'AUTHENTICATED', 'UNKNOWN', 'ERROR', 'NETWORK_ERROR',
    'TIMEOUT', 'CANCELLED', 'BUSINESS_QUERY_FAILED',
    'BUSINESS_RESPONSE_INVALID', 'REDIRECT_BLOCKED', 'QUOTA_EXCEEDED',
    'CONTENT_REFUSED', 'INTERRUPTED', 'NOT_FOUND',
}

# States that must never be reported as a successful empty list.
FAILURE_STATUSES = {
    'AUTH_REQUIRED', 'FORBIDDEN', 'RATE_LIMITED', 'MAINTENANCE', 'HTTP_ERROR',
    'BROWSER_REQUIRED', 'UNKNOWN', 'ERROR', 'NETWORK_ERROR', 'TIMEOUT',
    'BUSINESS_QUERY_FAILED', 'BUSINESS_RESPONSE_INVALID', 'REDIRECT_BLOCKED',
    'QUOTA_EXCEEDED', 'CONTENT_REFUSED', 'INTERRUPTED', 'NOT_FOUND',
}

# States for which result-envelope-v1 requires an ``error`` object.
ERROR_OBJECT_STATUSES = FAILURE_STATUSES

# States that may carry a complete business result.
COMPLETE_STATUSES = {
    'OK', 'EMPTY', 'AUTHENTICATED', 'PUBLIC_READABLE', 'READABLE_AUTH_UNKNOWN',
}

# Legacy gateway statuses still reach direct read_page callers.  They are
# evaluated through the v0.2 state without changing the legacy output value.
LEGACY_STATUS_ALIASES = {
    'PAGE_READ_VERIFIED': 'READABLE_AUTH_UNKNOWN',
    'JSON_READ_VERIFIED': 'UNKNOWN',
    'DIRECTORY_READ_VERIFIED': 'OK',
}


def canonical_status(value):
    return LEGACY_STATUS_ALIASES.get(value, value)

# source-evidence-v1 uses its own, smaller state enum.
EVIDENCE_STATES = {
    'OK': 'OK',
    'EMPTY': 'EMPTY',
    'PARTIAL': 'PARTIAL',
    'AUTH_REQUIRED': 'AUTH_REQUIRED',
    'FORBIDDEN': 'FORBIDDEN',
    'RATE_LIMITED': 'RATE_LIMITED',
    'MAINTENANCE': 'MAINTENANCE',
    'HTTP_ERROR': 'UPSTREAM_ERROR',
    'BROWSER_REQUIRED': 'UPSTREAM_ERROR',
    'NETWORK_ERROR': 'NETWORK_ERROR',
    'TIMEOUT': 'NETWORK_ERROR',
    'INTERRUPTED': 'NETWORK_ERROR',
    'UNKNOWN': 'UNKNOWN',
    'ERROR': 'UNKNOWN',
    'BUSINESS_QUERY_FAILED': 'UPSTREAM_ERROR',
    'BUSINESS_RESPONSE_INVALID': 'UPSTREAM_ERROR',
    'REDIRECT_BLOCKED': 'UPSTREAM_ERROR',
    'QUOTA_EXCEEDED': 'UPSTREAM_ERROR',
    'CONTENT_REFUSED': 'UPSTREAM_ERROR',
    'NOT_FOUND': 'UPSTREAM_ERROR',
    'CANCELLED': 'UNKNOWN',
}

# Explicit category mapping for envelope states that are not registered error
# codes.  It keeps ``status=NETWORK_ERROR`` classified as NETWORK rather than
# falling back to INTERNAL.
STATUS_CATEGORIES = {
    'HTTP_ERROR': ('UPSTREAM_ERROR', 'UPSTREAM_CONTRACT'),
    'BROWSER_REQUIRED': ('BROWSER_REQUIRED', 'UPSTREAM_CONTRACT'),
    'UNKNOWN': ('UNKNOWN', 'INTERNAL'),
    'ERROR': ('ERROR', 'INTERNAL'),
    'NETWORK_ERROR': ('NETWORK_ERROR', 'NETWORK'),
    'TIMEOUT': ('TIMEOUT', 'NETWORK'),
    'INTERRUPTED': ('INTERRUPTED', 'NETWORK'),
    'BUSINESS_QUERY_FAILED': ('BUSINESS_QUERY_FAILED', 'UPSTREAM_CONTRACT'),
    'BUSINESS_RESPONSE_INVALID': ('BUSINESS_RESPONSE_INVALID', 'UPSTREAM_CONTRACT'),
    'REDIRECT_BLOCKED': ('REDIRECT_BLOCKED', 'UPSTREAM_CONTRACT'),
    'QUOTA_EXCEEDED': ('QUOTA_EXCEEDED', 'RATE_LIMIT'),
    'CONTENT_REFUSED': ('CONTENT_REFUSED', 'UPSTREAM_CONTRACT'),
    'NOT_FOUND': ('NOT_FOUND', 'UPSTREAM_CONTRACT'),
}

CATEGORIES = {
    'INPUT': {'retryable': False, 'user_action': 'FIX_INPUT',
              'message': '请求参数不完整或不受支持，请修正后重试。'},
    'AUTH': {'retryable': False, 'user_action': 'REAUTHENTICATE',
             'message': '需要重新完成官方认证后才能继续。'},
    'PERMISSION': {'retryable': False, 'user_action': 'CONTACT_SUPPORT',
                   'message': '当前账号没有该操作所需的权限。'},
    'RATE_LIMIT': {'retryable': True, 'user_action': 'WAIT',
                   'message': '来源暂时限流，请稍后重试。'},
    'MAINTENANCE': {'retryable': True, 'user_action': 'WAIT',
                    'message': '来源正在维护或暂时不可用，请稍后重试。'},
    'NETWORK': {'retryable': True, 'user_action': 'RETRY',
                'message': '网络或本地网关暂时不可用，不能据此判断远端业务状态。'},
    'UPSTREAM_CONTRACT': {'retryable': False, 'user_action': 'CHECK_REMOTE_RECORD',
                          'message': '来源响应已不符合已登记的契约，请核对官方记录。'},
    'COMPLETENESS': {'retryable': True, 'user_action': 'RETRY',
                     'message': '本次结果不完整或快照已失效，请重新查询。'},
    'CONFLICT': {'retryable': False, 'user_action': 'FIX_INPUT',
                 'message': '账号、计划或本地状态已变化，请重新开始该操作。'},
    'NOT_IMPLEMENTED': {'retryable': False, 'user_action': 'CONTACT_SUPPORT',
                        'message': '当前版本尚未实现该项能力。'},
    'CANCELLED': {'retryable': False, 'user_action': 'NONE',
                  'message': '操作已取消；取消不代表远端写入被撤销。'},
    'INTERNAL': {'retryable': False, 'user_action': 'CONTACT_SUPPORT',
                 'message': '本地状态或实现异常，操作未完成。'},
}

# Runtime mirror of contracts/v0.2/error-classification.json ``codes``.
ERROR_CODES = {
    'ACTION_ACCOUNT_MISMATCH': 'CONFLICT',
    'RENEWAL_ACCOUNT_CHANGED_PREPARE_AGAIN': 'CONFLICT',
    'RENEWAL_CONTRACT_CHANGED_PREPARE_AGAIN': 'CONFLICT',
    'ACTION_ADAPTER_CHANGED': 'CONFLICT',
    'ACTION_ALREADY_CLAIMED': 'CONFLICT',
    'ACTION_ID_CONFLICT': 'CONFLICT',
    'ACTION_LOG_CORRUPT': 'INTERNAL',
    'RENEWAL_PREVIEW_FAILED': 'INTERNAL',
    'ACTION_LOG_DECRYPT_FAILED': 'INTERNAL',
    'ACTION_LOG_ENCRYPT_FAILED': 'INTERNAL',
    'ACTION_NOT_FOUND': 'INTERNAL',
    'ACTION_PLAN_EXPIRED': 'CONFLICT',
    'ACTION_PREFLIGHT_FAILED': 'INTERNAL',
    'ACTION_RESULT_CHANGED_RECHECK': 'CONFLICT',
    'ACTION_STORE_BUSY': 'CONFLICT',
    'ADAPTER_REQUIRED': 'NOT_IMPLEMENTED',
    'AUTH_REQUIRED': 'AUTH',
    'AuthenticationRequired': 'AUTH',
    'BROWSER_NAVIGATION_FAILED': 'NETWORK',
    'BROWSER_RUNTIME_UNAVAILABLE': 'NETWORK',
    'BROWSER_SESSION_DECRYPT_FAILED': 'AUTH',
    'BROWSER_SESSION_NOT_FOUND': 'AUTH',
    'BROWSER_SSO_NOT_VERIFIED': 'AUTH',
    'CAMPUS_GATEWAY_UNAVAILABLE': 'NETWORK',
    'CANCELLED': 'CANCELLED',
    'CURSOR_EXPIRED': 'COMPLETENESS',
    'CURSOR_QUERY_MISMATCH': 'COMPLETENESS',
    'RENEWAL_LOAN_PAGE_REPEATED': 'COMPLETENESS',
    'RENEWAL_LOAN_PAGINATION_UNVERIFIED': 'COMPLETENESS',
    'RENEWAL_PAGE_DISAGREES_WITH_API': 'COMPLETENESS',
    'FORBIDDEN': 'PERMISSION',
    'GATEWAY_EXITED': 'NETWORK',
    'INVALID_ACTION_TRANSITION': 'CONFLICT',
    'INVALID_ARGUMENTS': 'INPUT',
    'INVALID_ARGUMENT_LENGTH': 'INPUT',
    'INVALID_ARGUMENT_RANGE': 'INPUT',
    'INVALID_ARGUMENT_TYPE': 'INPUT',
    'INVALID_ARGUMENT_VALUE': 'INPUT',
    'INVALID_DATE': 'INPUT',
    'INVALID_DATE_RANGE': 'INPUT',
    'INVALID_JSON_POINTER': 'INPUT',
    'INVALID_LIBRARY_QUERY': 'INPUT',
    'INVALID_LIBRARY_SELECTOR': 'INPUT',
    'INVALID_OPERATION_ID': 'INPUT',
    'INVALID_SOURCE_URL': 'INPUT',
    'INVALID_TODO_SERVICES': 'INPUT',
    'RENEWAL_NOT_ELIGIBLE': 'INPUT',
    'RENEWAL_OVERDUE': 'INPUT',
    'RENEWAL_RECORD_NOT_FOUND': 'INPUT',
    'RENEWAL_RECORD_NOT_UNIQUE': 'INPUT',
    'InterfaceDriftError': 'UPSTREAM_CONTRACT',
    'JSON_POINTER_NOT_FOUND': 'INPUT',
    'JSON_POINTER_REQUIRES_JSON': 'INPUT',
    'LIBRARY_HISTORY_LIMIT': 'UPSTREAM_CONTRACT',
    'LIBRARY_LOAN_DATE_INVALID': 'UPSTREAM_CONTRACT',
    'LIBRARY_LOAN_PAGE_FAILED': 'UPSTREAM_CONTRACT',
    'RENEWAL_CONTRACT_INVALID': 'UPSTREAM_CONTRACT',
    'RENEWAL_CONTRACT_EVIDENCE_UNVERIFIED': 'UPSTREAM_CONTRACT',
    'RENEWAL_LOAN_LIST_LIMIT': 'UPSTREAM_CONTRACT',
    'LIBRARY_LOAN_TIME_INVALID': 'UPSTREAM_CONTRACT',
    'LIBRARY_NETWORK_ERROR': 'NETWORK',
    'LIBRARY_QUERY_FAILED': 'UPSTREAM_CONTRACT',
    'LIBRARY_REDIRECT_BLOCKED': 'UPSTREAM_CONTRACT',
    'LIBRARY_RESPONSE_INVALID': 'UPSTREAM_CONTRACT',
    'LIBRARY_RESPONSE_TOO_LARGE': 'UPSTREAM_CONTRACT',
    'LIBRARY_WRITES_DISABLED': 'PERMISSION',
    'LIBRARY_RENEW_DISABLED': 'PERMISSION',
    'MAINTENANCE': 'MAINTENANCE',
    'MISSING_ARGUMENT': 'INPUT',
    'MODEL_AUTH_REJECTED': 'AUTH',
    'MODEL_CONTENT_REFUSED': 'UPSTREAM_CONTRACT',
    'MODEL_FORBIDDEN': 'PERMISSION',
    'MODEL_NETWORK_ERROR': 'NETWORK',
    'MODEL_NOT_FOUND': 'UPSTREAM_CONTRACT',
    'MODEL_QUOTA_EXCEEDED': 'RATE_LIMIT',
    'MODEL_RATE_LIMITED': 'RATE_LIMIT',
    'MODEL_RESPONSE_INVALID': 'UPSTREAM_CONTRACT',
    'MODEL_STREAM_INTERRUPTED': 'NETWORK',
    'MODEL_TIMEOUT': 'NETWORK',
    'MULTIPLE_RESOURCE_SELECTORS': 'INPUT',
    'NON_TSINGHUA_HTTPS_URL': 'PERMISSION',
    'PREPARE_CONCRETE_ACTION_BEFORE_SUBMIT': 'INPUT',
    'PROVIDE_ONE_SERVICE_ID_OR_URL': 'INPUT',
    'PermissionError': 'PERMISSION',
    'ProviderMaintenance': 'MAINTENANCE',
    'ProviderNetworkError': 'NETWORK',
    'ProviderNotConfigured': 'NOT_IMPLEMENTED',
    'ProviderRateLimited': 'RATE_LIMIT',
    'RATE_LIMITED': 'RATE_LIMIT',
    'REQUEST_ENCODING_NOT_REGISTERED': 'CONFLICT',
    'REQUEST_METHOD_INVALID': 'CONFLICT',
    'REQUEST_SEQUENCE_REQUIRED': 'CONFLICT',
    'RESULT_TOO_LARGE_SELECT_NARROWER_SOURCE': 'UPSTREAM_CONTRACT',
    'ROOM_EXISTING_OVERLAP': 'CONFLICT',
    'ROOM_RECORD_RESOURCE_UNVERIFIED': 'UPSTREAM_CONTRACT',
    'ROOM_RULE_UNVERIFIED': 'UPSTREAM_CONTRACT',
    'SEAT_MAP_UNAVAILABLE': 'UPSTREAM_CONTRACT',
    'SEAT_UNAVAILABLE': 'CONFLICT',
    'SPACE_LOGIN_FAILED': 'AUTH',
    'RENEWAL_LOGIN_FAILED': 'AUTH',
    'SPACE_PAGE_FAILED': 'UPSTREAM_CONTRACT',
    'SPACE_QUERY_REJECTED': 'UPSTREAM_CONTRACT',
    'SPACE_READ_FAILED': 'UPSTREAM_CONTRACT',
    'SPACE_TIME_INVALID': 'UPSTREAM_CONTRACT',
    'SPECIFIC_HUMAN_AUTHORIZATION_REQUIRED': 'INTERNAL',
    'SPORTS_BUSY': 'CONFLICT',
    'SPORTS_CANCELLATION_NOT_AVAILABLE': 'NOT_IMPLEMENTED',
    'SPORTS_DISPATCH_UNOBSERVED': 'NETWORK',
    'SPORTS_EXISTING_MATCHING_RESERVATION': 'CONFLICT',
    'SPORTS_EXISTING_UNPAID_ORDER': 'CONFLICT',
    'SPORTS_FEE_CHANGED': 'CONFLICT',
    'SPORTS_FEE_NOT_VERIFIED': 'UPSTREAM_CONTRACT',
    'SPORTS_FORM_DEPLOYMENT_UNKNOWN': 'UPSTREAM_CONTRACT',
    'SPORTS_LOCATION_TREE_LIMIT': 'UPSTREAM_CONTRACT',
    'SPORTS_MODE_MISMATCH': 'INPUT',
    'SPORTS_NETWORK_ERROR': 'NETWORK',
    'SPORTS_ORDER_SEARCH_INCOMPLETE': 'UPSTREAM_CONTRACT',
    'SPORTS_OWN_RECORD_NOT_FOUND': 'UPSTREAM_CONTRACT',
    'SPORTS_QUERY_REJECTED': 'UPSTREAM_CONTRACT',
    'SPORTS_RECORD_CHANGED_REFRESH': 'CONFLICT',
    'SPORTS_RESPONSE_INVALID': 'UPSTREAM_CONTRACT',
    'SPORTS_SCENE_SELECTOR_REQUIRED': 'UPSTREAM_CONTRACT',
    'SPORTS_SESSION_DATE_MISMATCH': 'UPSTREAM_CONTRACT',
    'SPORTS_SESSION_NOT_FOUND': 'INPUT',
    'SPORTS_SESSION_TIME_AMBIGUOUS': 'UPSTREAM_CONTRACT',
    'SPORTS_SITE_NOT_IN_SELECTED_ROOM': 'UPSTREAM_CONTRACT',
    'SPORTS_SLOT_NOT_VERIFIED': 'UPSTREAM_CONTRACT',
    'SPORTS_SLOT_UNAVAILABLE': 'CONFLICT',
    'SPORTS_VERIFICATION_RULE_UNKNOWN': 'UPSTREAM_CONTRACT',
    'SPORTS_WRITES_DISABLED': 'PERMISSION',
    'STALE_SNAPSHOT_TAKE_NEW_SNAPSHOT': 'COMPLETENESS',
    'STATE_ACCOUNT_MISMATCH': 'CONFLICT',
    'TODOS_DISABLED': 'PERMISSION',
    'TODO_CACHE_CORRUPT': 'INTERNAL',
    'TODO_CACHE_ENCRYPT_FAILED': 'INTERNAL',
    'TODO_CACHE_UNREADABLE': 'INTERNAL',
    'TRUSTED_AUTHORIZATION_EVIDENCE_REQUIRED': 'INTERNAL',
    'TWO_FACTOR_REQUIRED': 'AUTH',
    'UNKNOWN_TOOL': 'INPUT',
    'UNREGISTERED_ACTION_OPERATION': 'INPUT',
    'UNREGISTERED_QUERY_OPERATION': 'INPUT',
    'UNREGISTERED_SPORTS_READ': 'INPUT',
    'USE_OFFICIAL_AUTH_FLOW_FOR_SECRETS': 'AUTH',
    'ValidationError': 'INPUT',
    'BROWSER_STATE_NOT_SAVED': 'INTERNAL',
    'BROWSER_STATE_SYNC_FAILED': 'INTERNAL',
    'HELPER_INVALID_RESPONSE': 'INTERNAL',
    'HELPER_UNAVAILABLE': 'INTERNAL',
    'INVALID_BROWSER_STATE': 'INTERNAL',
    'LEARN_ACCOUNT_REQUIRED': 'INPUT',
    'LEARN_SESSION_NOT_VERIFIED': 'AUTH',
    'SECURE_ACCOUNT_MISMATCH': 'CONFLICT',
    'SECURE_CRYPTO_UNAVAILABLE': 'INTERNAL',
    'SECURE_ENVELOPE_CORRUPT': 'INTERNAL',
    'SECURE_INVALID_RECORD_ID': 'INTERNAL',
    'SECURE_KEY_LOCATION': 'INTERNAL',
    'SECURE_KEY_MISMATCH': 'INTERNAL',
    'SECURE_KEY_NOT_CONFIGURED': 'INTERNAL',
    'SECURE_KEY_UNAVAILABLE': 'INTERNAL',
    'SECURE_KEY_UNREADABLE': 'INTERNAL',
    'SECURE_LEGACY_UNAVAILABLE': 'INTERNAL',
    'SECURE_PAYLOAD_INVALID': 'INTERNAL',
    'SECURE_PURPOSE_MISMATCH': 'CONFLICT',
    'SECURE_RECORD_MISMATCH': 'CONFLICT',
    'SECURE_REVISION_CONFLICT': 'CONFLICT',
    'SECURE_SERVICE_MISMATCH': 'CONFLICT',
    'SECURE_STORE_BUSY': 'CONFLICT',
    'SECURE_STORE_CORRUPT': 'INTERNAL',
    'SECURE_WRITE_FAILED': 'INTERNAL',
    'STATE_BACKUP_FAILED': 'INTERNAL',
    'STATE_BUSY': 'CONFLICT',
    'STATE_CONFLICT': 'CONFLICT',
    'STATE_CORRUPT': 'INTERNAL',
    'STATE_DECRYPT_FAILED': 'INTERNAL',
    'STATE_ENCRYPT_FAILED': 'INTERNAL',
    'STATE_READ_FAILED': 'INTERNAL',
    'STATE_REVISION_ROLLBACK': 'CONFLICT',
    'STATE_VERSION_UNSUPPORTED': 'INTERNAL',
    'STATE_WRITE_FAILED': 'INTERNAL',
    'SUBMISSION_ASSIGNMENT_CLOSED': 'CONFLICT',
    'SUBMISSION_ASSIGNMENT_NOT_FOUND': 'INPUT',
    'SUBMISSION_DEADLINE_UNVERIFIED': 'CONFLICT',
    'SUBMISSION_ERROR': 'INTERNAL',
    'SUBMISSION_EXISTING_SUBMISSION': 'CONFLICT',
    'SUBMISSION_FILE_CHANGED': 'CONFLICT',
    'SUBMISSION_FILE_NOT_FOUND': 'INPUT',
    'SUBMISSION_FILE_UNSAFE': 'INPUT',
    'SUBMISSION_INVALID_ARGUMENTS': 'INPUT',
    'SUBMISSION_LIMIT_EXCEEDED': 'INPUT',
    'SUBMISSION_LIVE_EVIDENCE_UNVERIFIED': 'NOT_IMPLEMENTED',
    'SUBMISSION_LOCAL_STATE_CORRUPT': 'INTERNAL',
    'SUBMISSION_LOCAL_STATE_UNAVAILABLE': 'INTERNAL',
    'SUBMISSION_PLAN_CONFLICT': 'CONFLICT',
    'SUBMISSION_PLAN_MISMATCH': 'CONFLICT',
    'SUBMISSION_PLATFORM_REJECTED': 'UPSTREAM_CONTRACT',
    'SUBMISSION_PRODUCTION_TRANSPORT_BLOCKED': 'NOT_IMPLEMENTED',
    'SUBMISSION_REPLACEMENT_UNVERIFIED': 'CONFLICT',
    'SUBMISSION_RESULT_UNVERIFIED': 'UPSTREAM_CONTRACT',
    'SUBMISSION_RULE_UNVERIFIED': 'CONFLICT',
    'SUBMISSION_SOURCE_UNAVAILABLE': 'NETWORK',
    'SUBMISSION_WRITES_DISABLED': 'PERMISSION',
    'SUBMISSION_WRITE_UNKNOWN': 'COMPLETENESS',
    'ACADEMICS_CALENDAR_MISMATCH': 'UPSTREAM_CONTRACT',
    'ACADEMICS_ERROR': 'INTERNAL',
    'ACADEMICS_EVENT_UNSUPPORTED': 'INPUT',
    'ACADEMICS_GRADE_CACHE_FORBIDDEN': 'PERMISSION',
    'ACADEMICS_GRADE_NOT_EVENT': 'INPUT',
    'ACADEMICS_INVALID_ARGUMENTS': 'INPUT',
    'ACADEMICS_INVALID_SEMESTER': 'INPUT',
    'ACADEMICS_INVALID_WEEK_PATTERN': 'UPSTREAM_CONTRACT',
    'ACADEMICS_PRODUCTION_TRANSPORT_BLOCKED': 'NOT_IMPLEMENTED',
    'ACADEMICS_SENSITIVE_REQUEST_REQUIRED': 'INPUT',
    'ACADEMICS_UNKNOWN_CAPABILITY': 'INPUT',
    'MODEL_IMAGE_INPUT_UNAVAILABLE': 'NOT_IMPLEMENTED',
    'MODEL_IMAGE_LIMIT_EXCEEDED': 'INPUT',
    'MODEL_IMAGE_MESSAGES_INVALID': 'INPUT',
    'MODEL_IMAGE_PROTOCOL_UNVERIFIED': 'NOT_IMPLEMENTED',
    'MODEL_IMAGE_REQUIRES_MEDIA_PIPELINE': 'NOT_IMPLEMENTED',
    'MODEL_IMAGE_SELECTION_INVALID': 'INPUT',
    'MODEL_IMAGE_STAGING_FAILED': 'INTERNAL',
    'MODEL_IMAGE_STAGING_MISMATCH': 'INTERNAL',
    'MODEL_IMAGE_TRANSPORT_UNAVAILABLE': 'NOT_IMPLEMENTED',
    'MODEL_IMAGE_UNSUPPORTED': 'INPUT',
    'MODEL_REQUIRED_FOR_IMAGE_INPUT': 'INPUT',
    'RESOURCE_ACCOUNT_MISMATCH': 'CONFLICT',
    'RESOURCE_CANCELLED': 'CANCELLED',
    'RESOURCE_CONFIRMATION_REQUIRED': 'INPUT',
    'RESOURCE_CONTENT_UNAVAILABLE': 'NOT_IMPLEMENTED',
    'RESOURCE_DISK_FULL': 'INTERNAL',
    'RESOURCE_DOWNLOAD_FAILED': 'UPSTREAM_CONTRACT',
    'RESOURCE_EXPORT_EMPTY': 'INPUT',
    'RESOURCE_EXPORT_EXISTS': 'CONFLICT',
    'RESOURCE_EXPORT_IN_REPOSITORY': 'PERMISSION',
    'RESOURCE_FILE_LOCKED': 'INTERNAL',
    'RESOURCE_HASH_CONFLICT': 'CONFLICT',
    'RESOURCE_INDEX_CORRUPT': 'INTERNAL',
    'RESOURCE_INDEX_INCOMPLETE': 'COMPLETENESS',
    'RESOURCE_INVALID_ARGUMENT': 'INPUT',
    'RESOURCE_LIMIT_EXCEEDED': 'INPUT',
    'RESOURCE_NOT_FOUND': 'INPUT',
    'RESOURCE_PLAN_CHANGED': 'CONFLICT',
    'RESOURCE_PLAN_EXPIRED': 'COMPLETENESS',
    'RESOURCE_SELECTION_INVALID': 'INPUT',
    'RESOURCE_SOURCE_INCOMPLETE': 'COMPLETENESS',
    'RESOURCE_SOURCE_UNSUPPORTED': 'INPUT',
    'RESOURCE_STORE_UNAVAILABLE': 'INTERNAL',
    'RESOURCE_TITLE_UNSAFE': 'INPUT',
    'RESOURCE_VERSION_CONFLICT': 'CONFLICT',
    'RESOURCE_WRITE_FAILED': 'INTERNAL',
    'RESOURCE_ERROR': 'INTERNAL',
    'MEDIA_BUNDLE_EXPIRED': 'COMPLETENESS',
    'MEDIA_BUNDLE_LIMIT': 'INPUT',
    'MEDIA_CANCELLED': 'CANCELLED',
    'MEDIA_CONTENT_FORBIDDEN': 'PERMISSION',
    'MEDIA_CONTENT_MISMATCH': 'INPUT',
    'MEDIA_DECODER_UNAVAILABLE': 'NOT_IMPLEMENTED',
    'MEDIA_DECODE_FAILED': 'INPUT',
    'MEDIA_EMPTY_FILE': 'INPUT',
    'MEDIA_ERROR': 'INTERNAL',
    'MEDIA_FILE_NOT_FOUND': 'INPUT',
    'MEDIA_FILE_UNREADABLE': 'PERMISSION',
    'MEDIA_INVALID_ARGUMENT': 'INPUT',
    'MEDIA_NOT_REGULAR_FILE': 'INPUT',
    'MEDIA_PATH_INVALID': 'INPUT',
    'MEDIA_PROCESSING_TIMEOUT': 'INPUT',
    'MEDIA_READ_FAILED': 'INTERNAL',
    'MEDIA_REFERENCE_ACCOUNT_MISMATCH': 'CONFLICT',
    'MEDIA_REFERENCE_CONSUMED': 'CONFLICT',
    'MEDIA_REFERENCE_CORRUPT': 'INTERNAL',
    'MEDIA_REFERENCE_EXPIRED': 'COMPLETENESS',
    'MEDIA_REFERENCE_NOT_FOUND': 'INTERNAL',
    'MEDIA_REFERENCE_PURPOSE_MISMATCH': 'CONFLICT',
    'MEDIA_REFERENCE_REVOKED': 'CONFLICT',
    'MEDIA_REFERENCE_TASK_MISMATCH': 'CONFLICT',
    'MEDIA_RESOURCE_LIMIT': 'INPUT',
    'MEDIA_SANITIZE_INCOMPLETE': 'INTERNAL',
    'MEDIA_SIZE_LIMIT': 'INPUT',
    'MEDIA_SOURCE_KIND_INVALID': 'INPUT',
    'MEDIA_STATE_UNAVAILABLE': 'INTERNAL',
    'MEDIA_TEMP_INSECURE': 'PERMISSION',
    'MEDIA_TEMP_IN_USE': 'CONFLICT',
    'MEDIA_TEMP_UNAVAILABLE': 'INTERNAL',
    'MEDIA_TRANSFER_NOT_AUTHORIZED': 'PERMISSION',
    'MEDIA_TYPE_UNSUPPORTED': 'INPUT',
    'MEDIA_WRITE_FAILED': 'INTERNAL'
}

CODE_PATTERN = re.compile(r'^[A-Za-z][A-Za-z0-9_]{2,80}$')
VERIFICATION_LEVELS = {
    'NONE', 'TRANSPORT_READ', 'PAGE_READ', 'AUTHENTICATED_PAGE', 'PUBLIC_PAGE',
    'DIRECTORY_READ', 'DIRECTORY_CACHE', 'BUSINESS_READ', 'LIVE_BUSINESS_READ',
    'LIVE_PARTIAL_DIRECTORY', 'REMOTE_RECORD_VERIFIED', 'LOCAL_EXECUTION_STATE',
    'SEE_OPERATION_EVIDENCE',
}
SECRET_QUERY = re.compile(
    r'password|cookie|token|csrf|ticket|session|signature|nonce|saml|relaystate|authorization|(?:^|_)sign$',
    re.I)
_UNSAFE_MESSAGE = re.compile(r'https?://|\b[A-Za-z]:\\|(?:^|\s)/(?:home|Users|root|tmp|var)\b|'
                             r'(?:token|cookie|password|signature|ticket|session)[=:]|[\x00-\x1f\x7f]', re.I)


def now(clock=None):
    """Current timezone-aware time (Asia/Shanghai by default)."""
    return (clock or (lambda: datetime.now(TZ)))()


def now_iso(clock=None):
    return now(clock).astimezone(TZ).isoformat(timespec='seconds')


def normalize_timestamp(value, fallback=None):
    """Return ISO 8601 with an explicit offset, or ``fallback``.

    Date-only and other unparseable values are not padded to midnight.  A
    date-only value is not a valid ``checked_at``; the caller should pass the
    actual observation time as ``fallback``.
    """
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=TZ)).astimezone(TZ).isoformat(timespec='seconds')
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(int(value))
    if not isinstance(value, str) or not value.strip():
        return fallback
    text = value.strip()
    if re.fullmatch(r'[1-9]\d{12}', text):
        return datetime.fromtimestamp(int(text) / 1000, TZ).isoformat(timespec='milliseconds')
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', text):
        return fallback
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?', text):
        return fallback
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.astimezone(TZ).isoformat(timespec='seconds')


def sanitize_url(url):
    """Remove credentials, fragments and sensitive query parameters.

    Returns ``None`` for an empty or non-HTTP(S) URL.  The result is safe to
    publish in source evidence; the raw URL must not be used as a fallback.
    """
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parsed = urlsplit(url.strip())
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        return None
    host = parsed.hostname
    netloc = host if port in (None, 80, 443) else f'{host}:{port}'
    path = re.sub(r';[^/]*session[^/]*', '', parsed.path, flags=re.I)
    query = urlencode([(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                       if not SECRET_QUERY.search(key)])
    return urlunsplit((parsed.scheme, netloc, path, query, ''))


def unknown_digest(value):
    """A stable, non-reversible summary for an unfamiliar enum value."""
    if value is None:
        return None
    text = value if isinstance(value, str) else repr(value)
    if not text:
        return None
    return 'UNKNOWN:' + hashlib.sha256(text.encode('utf-8', 'replace')).hexdigest()[:12]


def canonical_enum(value, mapping, *, default='UNKNOWN'):
    """Map a known value; otherwise return the default plus a safe digest."""
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        if value in mapping:
            return mapping[value], None
        if isinstance(value, str) and value.isdigit() and int(value) in mapping:
            return mapping[int(value)], None
        if isinstance(value, int) and str(value) in mapping:
            return mapping[str(value)], None
    return default, unknown_digest(value)


def evidence_state(status):
    return EVIDENCE_STATES.get(canonical_status(status), 'UNKNOWN')


def service_domain(service_id):
    return SERVICE_DOMAINS.get(str(service_id), 'UNKNOWN')


def classify_http(http_status=None, authentication_state=None, legacy_status=None,
                  maintenance=False, public_read=False):
    """Classify a page/HTTP observation without trusting HTTP 200 as success."""
    http = http_status if type(http_status) is int else None
    if http == 429:
        status, auth = 'RATE_LIMITED', 'UNKNOWN'
    elif http == 403 or authentication_state == 'FORBIDDEN':
        status, auth = 'FORBIDDEN', 'FORBIDDEN'
    elif http == 401 or authentication_state in ('EXPIRED', 'LOGIN_REQUIRED') or legacy_status == 'AUTH_REQUIRED':
        status, auth = 'AUTH_REQUIRED', 'EXPIRED'
    elif maintenance:
        status, auth = 'MAINTENANCE', 'UNKNOWN'
    elif http is not None and http >= 400:
        status, auth = 'HTTP_ERROR', 'UNKNOWN'
    elif legacy_status == 'BROWSER_REQUIRED':
        status, auth = 'BROWSER_REQUIRED', 'UNKNOWN'
    elif authentication_state == 'AUTHENTICATED':
        status, auth = 'AUTHENTICATED', 'AUTHENTICATED'
    elif legacy_status == 'PAGE_READ_VERIFIED':
        status, auth = ('PUBLIC_READABLE', 'PUBLIC') if public_read else ('READABLE_AUTH_UNKNOWN', 'UNKNOWN')
    else:
        status, auth = 'UNKNOWN', 'UNKNOWN'
    level = ('AUTHENTICATED_PAGE' if status == 'AUTHENTICATED' else
             'PUBLIC_PAGE' if status == 'PUBLIC_READABLE' else
             'PAGE_READ' if status == 'READABLE_AUTH_UNKNOWN' else
             'TRANSPORT_READ' if legacy_status == 'JSON_READ_VERIFIED' and http is not None and 200 <= http < 300 else
             'NONE')
    warnings = []
    if status == 'BROWSER_REQUIRED':
        warnings.append('DYNAMIC_CONTENT_REQUIRES_BROWSER')
    return {'status': status, 'auth_state': auth, 'evidence_state': evidence_state(status),
            'verification_level': level, 'http_status': http, 'warnings': warnings}


def safe_errors(values):
    """Keep only stable error codes; never publish raw exception text."""
    if values is None:
        return []
    if isinstance(values, (str, bytes)):
        values = [values]
    out = set()
    for item in values:
        code = item.get('code') if isinstance(item, dict) else item
        if isinstance(code, str) and CODE_PATTERN.fullmatch(code):
            out.add(code)
        elif code not in (None, ''):
            out.add('SOURCE_ERROR')
    return sorted(out)


def completeness(status, source_complete=False, snapshot_complete=True,
                 missing_pages=(), source_errors=(), warnings=()):
    """The three v0.2 completeness flags, derived without optimism.

    ``source_complete`` means every declared source page was read.
    ``snapshot_complete`` means the local paginated snapshot was fully returned.
    ``complete`` means the requested business scope is complete; it can never
    be true while either of the other two is false.
    """
    missing = set()
    invalid_missing = False
    for item in (missing_pages or ()):
        if type(item) is int and item >= 1:
            missing.add(item)
        elif isinstance(item, str) and re.fullmatch(r'[1-9][0-9]{0,9}', item.strip()):
            missing.add(int(item.strip()))
        else:
            invalid_missing = True
    missing = sorted(missing)
    errors = safe_errors(source_errors)
    status = canonical_status(status)
    source = bool(source_complete) and status not in FAILURE_STATUSES and status != 'PARTIAL'
    source = source and not missing and not errors and not invalid_missing
    snapshot = bool(snapshot_complete)
    complete = bool(source and snapshot and status in COMPLETE_STATUSES)
    reasons = []
    if status in FAILURE_STATUSES:
        reasons.append('SOURCE_' + status)
    if status == 'PARTIAL':
        reasons.append('SOURCE_PARTIAL')
    if missing:
        reasons.append('MISSING_SOURCE_PAGES')
    if invalid_missing:
        reasons.append('INVALID_MISSING_PAGES')
    if errors:
        reasons.append('SOURCE_ERRORS_PRESENT')
    if not snapshot:
        reasons.append('SNAPSHOT_INCOMPLETE')
    flags = set()
    for warning in warnings or ():
        if isinstance(warning, str) and 0 < len(warning) <= 200:
            flags.add(warning)
        elif warning not in (None, ''):
            flags.add('UNVERIFIED_WARNING_REDACTED')
    flags.update(reasons)
    return {'source_complete': source, 'snapshot_complete': snapshot,
            'complete': complete, 'warnings': sorted(flags),
            'partial_reasons': sorted(set(reasons)),
            'missing_pages': missing, 'source_errors': errors}


def read_scope(kind='PAGE', *, offset=0, limit=1, pages_read=0,
               expected_pages=None, query_time=None, window=None):
    """Build the source-evidence read_scope object."""
    if kind not in ('PAGE', 'WINDOW', 'FULL_LIST', 'RECORD'):
        raise ValueError('INVALID_READ_SCOPE_KIND')
    if type(offset) is not int or offset < 0:
        raise ValueError('INVALID_READ_SCOPE_OFFSET')
    if type(limit) is not int or not 1 <= limit <= 100000:
        raise ValueError('INVALID_READ_SCOPE_LIMIT')
    if type(pages_read) is not int or pages_read < 0:
        raise ValueError('INVALID_READ_SCOPE_PAGES')
    if expected_pages is not None and (type(expected_pages) is not int or expected_pages < 0):
        raise ValueError('INVALID_READ_SCOPE_EXPECTED_PAGES')
    if window is not None:
        if not isinstance(window, dict) or set(window) - {'start', 'end'}:
            raise ValueError('INVALID_READ_SCOPE_WINDOW')
        window = {'start': window.get('start'), 'end': window.get('end')}
        if any(value is not None and not isinstance(value, str) for value in window.values()):
            raise ValueError('INVALID_READ_SCOPE_WINDOW')
    return {'kind': kind, 'offset': offset, 'limit': limit,
            'pages_read': pages_read, 'expected_pages': expected_pages,
            'query_time': normalize_timestamp(query_time, now_iso()), 'window': window}


def public_error(code, *, category=None, retry_after_ms=None, message=None):
    """A public error object with no exception text, host name or path.

    Unregistered codes collapse to ``UNKNOWN``/``INTERNAL`` unless an explicit
    category is supplied.  ``message`` is only accepted when it is short and
    free of URLs, absolute paths and secret markers.
    """
    text = code if isinstance(code, str) and CODE_PATTERN.fullmatch(code) else 'UNKNOWN'
    if category is None:
        category = ERROR_CODES.get(text)
        if category is None and text in STATUS_CATEGORIES:
            text, category = STATUS_CATEGORIES[text]
        if category is None:
            text, category = 'UNKNOWN', 'INTERNAL'
    if category not in CATEGORIES:
        text, category = 'UNKNOWN', 'INTERNAL'
    rule = CATEGORIES[category]
    safe_message = rule['message']
    if isinstance(message, str) and message.strip() and len(message) <= 200 and not _UNSAFE_MESSAGE.search(message):
        safe_message = message.strip()
    result = {'code': text, 'category': category, 'message': safe_message,
              'retryable': rule['retryable'], 'automatic_replay_allowed': False,
              'user_action': rule['user_action']}
    if type(retry_after_ms) is int and retry_after_ms >= 0:
        result['retry_after_ms'] = retry_after_ms
    return result


def classify_error(error):
    """Translate an exception without copying its text into the result."""
    code = getattr(error, 'code', None)
    if not isinstance(code, str):
        code = type(error).__name__
    retry_after = getattr(error, 'retry_after_ms', None)
    return public_error(code, retry_after_ms=retry_after if type(retry_after) is int else None)


def aggregate_completeness(sources):
    """Aggregate per-source envelopes; one failed source makes the whole partial."""
    rows = list(sources or ())
    if not rows:
        return {'complete': False, 'source_complete': False, 'snapshot_complete': True,
                'partial_reasons': ['NO_SOURCE'], 'sources': 0}
    flags = [completeness(row.get('status', 'UNKNOWN'), row.get('source_complete', False),
                          row.get('snapshot_complete', True), row.get('missing_pages', ()),
                          row.get('source_errors', ()), row.get('warnings', ()))
             for row in rows]
    reasons = sorted({reason for flag in flags for reason in flag['partial_reasons']})
    return {'complete': all(flag['complete'] for flag in flags),
            'source_complete': all(flag['source_complete'] for flag in flags),
            'snapshot_complete': all(flag['snapshot_complete'] for flag in flags),
            'partial_reasons': reasons, 'sources': len(rows)}


def finalize(envelope, *, status, source_complete=False, snapshot_complete=True,
             missing_pages=(), source_errors=(), query_time=None,
             scope=None, warnings=()):
    """Apply the completeness triad and error object to an envelope copy."""
    result = dict(envelope)
    flags = completeness(status, source_complete, snapshot_complete,
                         missing_pages, source_errors, warnings)
    result['status'] = status
    result['source_complete'] = flags['source_complete']
    result['snapshot_complete'] = flags['snapshot_complete']
    result['complete'] = flags['complete']
    result['missing_pages'] = flags['missing_pages']
    if flags['source_errors']:
        result['source_errors'] = flags['source_errors']
    result['warnings'] = sorted({warning for warning in list(result.get('warnings') or ())
                                 if isinstance(warning, str) and 0 < len(warning) <= 200} |
                                set(flags['warnings']))
    result['partial_reasons'] = flags['partial_reasons']
    result['query_time'] = normalize_timestamp(
        query_time or result.get('query_time') or result.get('checked_at'), now_iso())
    if scope is not None:
        result['read_scope'] = scope
    result['source_state'] = evidence_state(status)
    if status in ERROR_OBJECT_STATUSES:
        result['error'] = public_error(status)
    return result


def source_evidence(*, source_id, service, operation, adapter_version, checked_at,
                    state, scope, row_count=0, upstream_total=None,
                    deduplicated_entries=0, source_complete=False, complete=False,
                    missing_pages=(), warnings=(), verification_level='NONE',
                    synthetic=True, url=None, http_status=None, auth_state='UNKNOWN',
                    content_sha256=None, rows=None):
    """Build a source-evidence-v1 object for an adapter or fixture.

    ``synthetic`` must be stated by the caller; this function never guesses
    whether a read was live or synthetic.  ``rows`` is optional and must be
    normalized data, never a raw page.
    """
    if not isinstance(source_id, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,120}', source_id):
        raise ValueError('INVALID_SOURCE_ID')
    if not isinstance(scope, dict):
        raise ValueError('INVALID_READ_SCOPE')
    operation = operation.strip() if isinstance(operation, str) else ''
    adapter_version = adapter_version.strip() if isinstance(adapter_version, str) else ''
    if not operation or len(operation) > 120:
        raise ValueError('INVALID_OPERATION')
    if not adapter_version or len(adapter_version) > 80:
        raise ValueError('INVALID_ADAPTER_VERSION')
    if verification_level not in VERIFICATION_LEVELS:
        raise ValueError('INVALID_VERIFICATION_LEVEL')
    if content_sha256 is not None and (not isinstance(content_sha256, str) or
                                       not re.fullmatch(r'[a-f0-9]{64}', content_sha256)):
        raise ValueError('INVALID_CONTENT_SHA256')
    if rows is not None and not isinstance(rows, (list, tuple)):
        raise ValueError('INVALID_ROWS')
    safe_link = sanitize_url(url)
    warning_list = list(warnings or ())
    if url and safe_link is None:
        warning_list.append('SOURCE_URL_UNVERIFIED')
    if safe_link is not None and len(safe_link) > 2000:
        safe_link = None
        warning_list.append('SOURCE_URL_TOO_LONG')
    domain = service_domain(service)
    if auth_state in ('EXPIRED', 'FORBIDDEN') and state in ('OK', 'EMPTY'):
        # An authentication failure can never be a successful empty read.
        state = 'AUTH_REQUIRED' if auth_state == 'EXPIRED' else 'FORBIDDEN'
    if 'UPSTREAM_TOTAL_UNKNOWN' in warning_list:
        upstream_total = None
    scope = read_scope(scope.get('kind', 'PAGE'), offset=scope.get('offset', 0),
                       limit=scope.get('limit', 1), pages_read=scope.get('pages_read', 0),
                       expected_pages=scope.get('expected_pages'),
                       query_time=scope.get('query_time'), window=scope.get('window'))
    flags = completeness(state, source_complete, True, missing_pages, (), warning_list)
    checked = normalize_timestamp(checked_at, now_iso())
    payload = {
        'source_id': source_id,
        'service_domain': domain,
        'operation': operation,
        'adapter_version': adapter_version,
        'checked_at': checked,
        'url': safe_link,
        'http_status': http_status if type(http_status) is int and 100 <= http_status <= 599 else None,
        'auth_state': auth_state if auth_state in ('AUTHENTICATED', 'PUBLIC', 'EXPIRED', 'FORBIDDEN', 'UNKNOWN') else 'UNKNOWN',
        'state': evidence_state(state),
        'read_scope': scope,
        'row_count': max(0, int(row_count or 0)),
        'upstream_total': upstream_total if type(upstream_total) is int and upstream_total >= 0 else None,
        'deduplicated_entries': max(0, int(deduplicated_entries or 0)),
        'source_complete': flags['source_complete'],
        'complete': flags['complete'] and bool(complete),
        'missing_pages': flags['missing_pages'],
        'warnings': flags['warnings'],
        'content_sha256': content_sha256,
        'verification_level': verification_level,
        'synthetic': bool(synthetic),
        'raw_payload_included': False,
    }
    if rows is not None:
        payload['rows'] = list(rows)
    return payload
