"""Classified, leak-free errors for the assignment submission boundary (5.3).

Codes are additive entries in ``contracts/v0.2/error-classification.json`` and
mirrored in ``source_state.ERROR_CODES`` so the facade classifies them instead
of collapsing them into ``UNKNOWN``/``INTERNAL``.  Detail strings never carry a
path, a URL, a file name or a platform response.
"""
from __future__ import annotations

import re

from ..bridge import CampusError

_DETAIL = re.compile(r'^[A-Za-z0-9_.:=,+-]{0,200}$')

# code -> (category, retryable, user_action, public message)
CODES = {
    'SUBMISSION_ERROR': (
        'INTERNAL', False, 'CONTACT_SUPPORT',
        '提交模块发生未分类的本地错误，操作未完成。',
    ),
    'SUBMISSION_PRODUCTION_TRANSPORT_BLOCKED': (
        'NOT_IMPLEMENTED', False, 'CONTACT_SUPPORT',
        '上传、草稿与正式提交的来源契约尚未核对；当前版本不接受生产传输。',
    ),
    'SUBMISSION_WRITES_DISABLED': (
        'PERMISSION', False, 'CONTACT_SUPPORT',
        '作业正式提交开关已关闭，未派发任何请求。',
    ),
    'SUBMISSION_INVALID_ARGUMENTS': (
        'INPUT', False, 'FIX_INPUT',
        '提交参数不在登记范围内。',
    ),
    'SUBMISSION_ASSIGNMENT_NOT_FOUND': (
        'INPUT', False, 'FIX_INPUT',
        '作业不可读取或不在当前账号的作业列表中。',
    ),
    'SUBMISSION_FILE_NOT_FOUND': (
        'INPUT', False, 'FIX_INPUT',
        '本地待提交文件不存在或不可读。',
    ),
    'SUBMISSION_FILE_UNSAFE': (
        'INPUT', False, 'FIX_INPUT',
        '待提交文件不是普通文件、为空、类型不符或超过限制。',
    ),
    'SUBMISSION_FILE_CHANGED': (
        'CONFLICT', False, 'FIX_INPUT',
        '文件内容在预览后发生变化，旧计划已失效。',
    ),
    'SUBMISSION_LIMIT_EXCEEDED': (
        'INPUT', False, 'FIX_INPUT',
        '文件数量或总大小超过本模块的工程上限。',
    ),
    'SUBMISSION_ASSIGNMENT_CLOSED': (
        'CONFLICT', False, 'FIX_INPUT',
        '作业已过截止时间，未派发任何请求。',
    ),
    'SUBMISSION_EXISTING_SUBMISSION': (
        'CONFLICT', False, 'FIX_INPUT',
        '该作业已有提交记录；未明确要求替换时拒绝覆盖。',
    ),
    'SUBMISSION_REPLACEMENT_UNVERIFIED': (
        'CONFLICT', False, 'CHECK_REMOTE_RECORD',
        '无法核对要替换的提交版本，不能默认覆盖。',
    ),
    'SUBMISSION_PLAN_MISMATCH': (
        'CONFLICT', False, 'FIX_INPUT',
        '本地计划、文件清单或账号与当前请求不一致。',
    ),
    'SUBMISSION_SOURCE_UNAVAILABLE': (
        'NETWORK', True, 'RETRY',
        '作业读取暂时不可用；不能据此判断远端提交状态，也不会自动重试写入。',
    ),
    'SUBMISSION_RESULT_UNVERIFIED': (
        'UPSTREAM_CONTRACT', False, 'CHECK_REMOTE_RECORD',
        '平台未提供足以证明最终提交的字段，结果仍需在官方页核对。',
    ),
    'SUBMISSION_WRITE_UNKNOWN': (
        'COMPLETENESS', True, 'CHECK_REMOTE_RECORD',
        '写入响应丢失或超时；结果未知，必须核对官方记录且不得自动重放。',
    ),
    'SUBMISSION_PLATFORM_REJECTED': (
        'UPSTREAM_CONTRACT', False, 'CHECK_REMOTE_RECORD',
        '平台拒绝了本次请求；已发出的写入不会被自动重放。',
    ),
    'SUBMISSION_LOCAL_STATE_CORRUPT': (
        'INTERNAL', False, 'CONTACT_SUPPORT',
        '本地提交记录已损坏，不能继续该计划。',
    ),
    'SUBMISSION_LOCAL_STATE_UNAVAILABLE': (
        'INTERNAL', False, 'CONTACT_SUPPORT',
        '本地提交记录不可读或不可写，未派发任何请求。',
    ),
}


class SubmissionError(CampusError):
    """Classified error; the action engine preserves the code it carries."""

    code = 'SUBMISSION_ERROR'

    def __init__(self, code=None, detail=''):
        if code is not None:
            self.code = code
        if self.code not in CODES:
            self.code = 'SUBMISSION_ERROR'
        self.category, self.retryable, self.user_action, self.message = CODES[self.code]
        self.detail = detail if _DETAIL.fullmatch(detail or '') else 'DETAIL_REDACTED'
        super().__init__(self.code)

    def as_error(self):
        return error_object(self.code)


def error_object(code):
    """Classified error object for a registered submission code."""
    if code not in CODES:
        code = 'SUBMISSION_ERROR'
    category, retryable, user_action, message = CODES[code]
    return {
        'code': code,
        'category': category,
        'message': message,
        'retryable': retryable,
        'automatic_replay_allowed': False,
        'user_action': user_action,
    }


def classification_map():
    """``code -> category`` for facades that must not call the frozen classifier."""
    return {code: rule[0] for code, rule in CODES.items()}


def classify(value):
    """Classify a submission exception or code without copying its text."""
    if isinstance(value, SubmissionError):
        return value.as_error()
    if isinstance(value, str) and value in CODES:
        return error_object(value)
    return error_object('SUBMISSION_ERROR')


__all__ = ['CODES', 'SubmissionError', 'classification_map', 'classify', 'error_object']
