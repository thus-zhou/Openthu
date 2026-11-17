"""Stable, leak-free errors for the read-only academics adapters.

Codes are module-local until the business facade (6.1) registers them in
``contracts/v0.2/error-classification.json``; that file belongs to the frozen
contract owner (2.3), so this module ships an explicit category map that must be
used before ``source_state.classify_error``.  ``classification_map`` keeps the
mapping inspectable and stops a module error from being collapsed into
``UNKNOWN``/``INTERNAL``.

Detail strings are restricted to short enum-like tokens: a source URL, an
exception text, a student identifier or a grade value must never appear in an
error message.
"""
from __future__ import annotations

import re

_DETAIL = re.compile(r'^[A-Za-z0-9_.:=,+-]{0,200}$')

# code -> (category, retryable, user_action, public message)
CODES = {
    'ACADEMICS_ERROR': (
        'INTERNAL', False, 'CONTACT_SUPPORT',
        '学业读取发生未分类的本地错误，操作未完成。',
    ),
    'ACADEMICS_PRODUCTION_TRANSPORT_BLOCKED': (
        'NOT_IMPLEMENTED', False, 'CONTACT_SUPPORT',
        '来源契约未核对前不接受非合成传输，避免把未验证来源当作已接通。',
    ),
    'ACADEMICS_SENSITIVE_REQUEST_REQUIRED': (
        'INPUT', False, 'FIX_INPUT',
        '读取课程成绩需要调用方针对该次查询明确请求。',
    ),
    'ACADEMICS_UNKNOWN_CAPABILITY': (
        'INPUT', False, 'FIX_INPUT',
        '请求的学业能力未登记；可选值为 timetable、exams、grades。',
    ),
    'ACADEMICS_INVALID_ARGUMENTS': (
        'INPUT', False, 'FIX_INPUT',
        '学业查询参数不在登记范围内，不能提交任意请求头、Cookie 或上游路径。',
    ),
    'ACADEMICS_INVALID_SEMESTER': (
        'INPUT', False, 'FIX_INPUT',
        '学期选择需要来源学期编号；同名学期不会被自动合并。',
    ),
    'ACADEMICS_INVALID_WEEK_PATTERN': (
        'UPSTREAM_CONTRACT', False, 'CHECK_REMOTE_RECORD',
        '周次、单双周或节次字段不符合已登记的形状，保留原始周期。',
    ),
    'ACADEMICS_CALENDAR_MISMATCH': (
        'UPSTREAM_CONTRACT', False, 'CHECK_REMOTE_RECORD',
        '校历、节次时间表或学期不一致，未展开为具体日期。',
    ),
    'ACADEMICS_EVENT_UNSUPPORTED': (
        'INPUT', False, 'FIX_INPUT',
        '该记录缺少生成事件所需的日期、时间或稳定标识。',
    ),
    'ACADEMICS_GRADE_NOT_EVENT': (
        'INPUT', False, 'FIX_INPUT',
        '课程成绩不会转换成日程事件或待办；需要单独的成绩读取请求。',
    ),
    'ACADEMICS_GRADE_CACHE_FORBIDDEN': (
        'PERMISSION', False, 'CONTACT_SUPPORT',
        '课程成绩不能写入未加密缓存，也不能进入公共摘要。',
    ),
}


class AcademicsError(Exception):
    """Base error; callers read ``code``/``category`` and ``as_error()``."""

    code = 'ACADEMICS_ERROR'

    def __init__(self, code=None, detail=''):
        if code is not None:
            self.code = code
        if self.code not in CODES:
            self.code = 'ACADEMICS_ERROR'
        self.category, self.retryable, self.user_action, self.message = CODES[self.code]
        self.detail = detail if _DETAIL.fullmatch(detail or '') else 'DETAIL_REDACTED'
        super().__init__(self.code)

    def as_error(self):
        return error_object(self.code)


def error_object(code):
    """Classified error object for a registered academics code."""
    if code not in CODES:
        code = 'ACADEMICS_ERROR'
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
    """Classify an academics exception or code without copying its text.

    A foreign exception becomes ``ACADEMICS_ERROR``/``INTERNAL``; it is never
    re-raised with its original message and never treated as a source success.
    """
    if isinstance(value, AcademicsError):
        return value.as_error()
    if isinstance(value, str) and value in CODES:
        return error_object(value)
    return error_object('ACADEMICS_ERROR')


__all__ = ['AcademicsError', 'CODES', 'classification_map', 'classify', 'error_object']
