'use strict';
/* Openthu local dashboard: pure display model (module 7.1).
 *
 * This file has no DOM and no network access.  Everything here turns a
 * projected `result-envelope-v1` object into display text, so the security
 * rules (text escaping, school-link allowlist, incomplete-is-not-success) can
 * be tested without a browser and reused by the renderer unchanged.
 *
 * Loaded as a classic script by index.html and exposed as
 * `window.OpenthuModel`; a CommonJS export is kept so the same functions can
 * be exercised from Node without a second copy.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.OpenthuModel = factory();
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  const VERSION = 'dashboard-model-v1';
  const API_BASE = '/api/v1';
  const PAGE_SIZE = 50;              // local-read-api-v1 page_size_default
  const REFRESH_MIN_SECONDS = 10;     // local-read-api-v1 refresh_min_seconds
  const PAGE_IDS = ['overview', 'courses', 'resources', 'calendar', 'library', 'health'];
  const PAGES = [
    {id: 'overview', label: '总览', title: '总览'},
    {id: 'courses', label: '课程', title: '课程'},
    {id: 'resources', label: '资料', title: '课程资料'},
    {id: 'calendar', label: '日程', title: '日程'},
    {id: 'library', label: '借阅预约', title: '借阅与预约'},
    {id: 'health', label: '健康', title: '来源健康'}
  ];
  const API = {
    health: API_BASE + '/health',
    services: API_BASE + '/services',
    service: API_BASE + '/services/',
    query: API_BASE + '/query/',
    todos: API_BASE + '/todos',
    calendar: API_BASE + '/calendar',
    resources: API_BASE + '/resources'
  };
  // First-party entries only.  The renderer re-checks each with isSchoolHttps
  // before creating an anchor; a constant here is not a trust decision.
  const OFFICIAL_ENTRIES = [
    {label: '图书馆主页', url: 'https://lib.tsinghua.edu.cn/'},
    {label: '图书馆座位预约', url: 'https://seat.lib.tsinghua.edu.cn/'},
    {label: '研读间预约', url: 'https://cab.lib.tsinghua.edu.cn/'},
    {label: '体育场馆', url: 'https://sports.tsinghua.edu.cn/'},
    {label: '网络学堂', url: 'https://learn.tsinghua.edu.cn/'}
  ];

  const STATES = {
    READY: {tone: 'ok', label: '完整读取', message: '结果已按当前范围完整读取。', next: '无'},
    EMPTY: {tone: 'empty', label: '没有记录', message: '没有符合条件的记录。',
            next: '调整筛选条件或稍后再看。'},
    PARTIAL: {tone: 'partial', label: '部分读取', message: '来源未完整读取，结果只覆盖已读取范围。',
              next: '稍后重试或缩小范围。'},
    LOGIN_REQUIRED: {tone: 'auth', label: '未登录', message: '需要先完成校园认证才能读取。',
                     next: '在原有受控入口完成认证后再刷新；本机看板不代登录。'},
    LOCAL_SESSION_REQUIRED: {tone: 'auth', label: '本地会话已失效',
                             message: '本地看板会话不存在或已过期。',
                             next: '重新打开本地看板建立新的本地会话；不需要重新登录学校。'},
    LIMITED: {tone: 'limited', label: '来源受限', message: '来源受限或读取超时。',
              next: '稍后重试；其他卡片不受影响。'},
    FORBIDDEN: {tone: 'error', label: '没有权限', message: '当前账号没有该权限。',
                next: '确认账号，或使用原有受控入口。'},
    MAINTENANCE: {tone: 'limited', label: '维护中', message: '学校服务正在维护。',
                  next: '稍后重试。'},
    ERROR: {tone: 'error', label: '读取失败', message: '读取失败。',
            next: '查看下一步提示后重试；未知不等于成功。'},
    UNKNOWN: {tone: 'unknown', label: '状态未知', message: '当前状态未知。',
              next: '稍后重试；未知不等于成功。'},
    // Local-only states: no API response produces these, but a page can need
    // an input before it may read, or a facade can report a switched-off
    // capability.  They are kept distinct from EMPTY and ERROR so the user is
    // never told "no records" when the truth is "not read" or "not enabled".
    NEEDS_INPUT: {tone: 'unknown', label: '需要选择', message: '还需要一个输入才能读取。',
                  next: '按页面提示选择后再读取；不把缺少输入当成空结果。'},
    NOT_ENABLED: {tone: 'unknown', label: '未启用', message: '该能力尚未启用。',
                  next: '使用原有受控入口查看能力状态，或稍后再试。'}
  };

  const TEXT_LIMIT = 2000;
  const CONTROL = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g;
  const BIDI = /[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]/g;
  const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
  const ISO_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/;
  const TERMINAL = new Set(['DONE', 'COMPLETED', 'CANCELLED', 'CLOSED', 'EXPIRED',
                            'SUBMITTED', 'GRADED', 'PAID', 'WITHDRAWN', 'REJECTED']);
  const STATUS_LABELS = {
    OK: '正常', EMPTY: '没有记录', PARTIAL: '部分读取', AUTH_REQUIRED: '需要认证',
    TIMEOUT: '读取超时', ERROR: '读取错误', UNKNOWN: '状态未知',
    OPEN: '待完成', PENDING: '待处理', IN_PROGRESS: '进行中', DONE: '已完成',
    COMPLETED: '已完成', SUBMITTED: '已提交', GRADED: '已批改',
    RETURNED: '已归还', BORROWED: '已借出', OVERDUE: '已逾期', DUE: '即将到期',
    CANCELLED: '已取消', CLOSED: '已关闭', EXPIRED: '已过期', PAID: '已支付',
    UNPAID: '待支付', REFUNDED: '已退款', REJECTED: '已拒绝', WITHDRAWN: '已撤回',
    WAITING: '等待中', ACTIVE: '有效', SUPERSEDED: '已被替代', STALE: '已过期',
    MISSING: '缺失'
  };
  const KIND_LABELS = {student: '学生', teacher: '教师', assistant: '助教', ta: '助教',
                       other: '其他', unknown: '未知'};
  const TYPE_BY_MIME = [
    ['application/pdf', 'PDF'],
    ['application/zip', '压缩包'], ['application/x-zip', '压缩包'],
    ['application/vnd.openxmlformats-officedocument.wordprocessingml', '文档'],
    ['application/msword', '文档'],
    ['application/vnd.openxmlformats-officedocument.spreadsheetml', '表格'],
    ['application/vnd.ms-excel', '表格'],
    ['application/vnd.openxmlformats-officedocument.presentationml', '演示'],
    ['application/vnd.ms-powerpoint', '演示'],
    ['application/json', '数据'], ['text/csv', '表格'],
    ['text/plain', '文本'], ['text/markdown', '文本']
  ];
  const TYPE_BY_EXT = {
    pdf: 'PDF', doc: '文档', docx: '文档', rtf: '文档', odt: '文档',
    xls: '表格', xlsx: '表格', csv: '表格', ods: '表格',
    ppt: '演示', pptx: '演示', odp: '演示',
    zip: '压缩包', rar: '压缩包', '7z': '压缩包', tar: '压缩包', gz: '压缩包',
    png: '图片', jpg: '图片', jpeg: '图片', webp: '图片', gif: '图片', svg: '图片',
    mp4: '视频', mov: '视频', mkv: '视频', webm: '视频',
    mp3: '音频', m4a: '音频', wav: '音频', flac: '音频',
    txt: '文本', md: '文本', json: '数据', xml: '数据', yaml: '数据', yml: '数据'
  };
  const RESOURCE_KINDS = ['PDF', '文档', '表格', '演示', '图片', '视频', '音频',
                          '压缩包', '文本', '数据', '其他'];
  const CALENDAR_LIMIT_WARNINGS = new Set([
    'TIME_NOT_FROM_PERIOD_TABLE', 'PERIOD_TABLE_MISSING',
    'CALENDAR_PERIOD_TABLE_SEMESTER_MISMATCH', 'PERIODS_MISSING'
  ]);

  function text(value, max) {
    if (value === null || value === undefined) return '';
    let out = String(value).replace(CONTROL, '').replace(BIDI, '');
    out = out.trim();
    const limit = max || TEXT_LIMIT;
    if (out.length > limit) out = out.slice(0, Math.max(0, limit - 1)) + '…';
    return out;
  }

  function asList(value) {
    if (!Array.isArray(value)) return [];
    return value.map(function (item) { return text(item, 200); }).filter(Boolean);
  }

  function intOrNull(value) {
    return Number.isInteger(value) ? value : null;
  }

  function normalise(value) {
    return text(value, TEXT_LIMIT).toLowerCase();
  }

  function pad(value) {
    return String(value).padStart(2, '0');
  }

  function partsOf(date, timeZone) {
    const formatter = new Intl.DateTimeFormat('en-CA', {
      timeZone: timeZone || 'Asia/Shanghai', year: 'numeric', month: '2-digit',
      day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false
    });
    const parts = {};
    formatter.formatToParts(date).forEach(function (part) {
      if (part.type !== 'literal') parts[part.type] = part.value;
    });
    return parts;
  }

  function parseSourceTime(value) {
    if (value === null || value === undefined) return NaN;
    if (value instanceof Date) return value.getTime();
    let raw = String(value).trim();
    if (!raw) return NaN;
    // A source DATE_TIME without an offset is a Shanghai wall-clock value in
    // local-read-api-v1.  Letting the browser parse it would make overdue and
    // display depend on the machine's timezone.
    if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/.test(raw)) {
      raw += '+08:00';
    }
    return Date.parse(raw);
  }

  function dateOf(value) {
    if (value === null || value === undefined || value === '') return null;
    if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
    if (typeof value === 'number') {
      const numeric = new Date(value);
      return Number.isNaN(numeric.getTime()) ? null : numeric;
    }
    const parsed = new Date(parseSourceTime(value));
    return Number.isNaN(parsed.getTime()) ? null : parsed;
  }

  function todayIso(value) {
    const date = dateOf(value || new Date());
    if (!date) return '';
    const parts = partsOf(date);
    return parts.year + '-' + parts.month + '-' + parts.day;
  }

  function formatTimestamp(value) {
    const raw = text(value, 40);
    if (DATE_RE.test(raw)) return raw + '（日期）';
    const date = dateOf(value);
    if (!date) return '时间未知';
    const parts = partsOf(date);
    const hour = parts.hour === '24' ? '00' : parts.hour;
    return parts.year + '-' + parts.month + '-' + parts.day + ' ' + hour + ':' + parts.minute;
  }

  function formatDate(value) {
    const raw = text(value, 40);
    if (!DATE_RE.test(raw)) return '日期未知';
    const date = new Date(raw + 'T12:00:00+08:00');
    if (Number.isNaN(date.getTime())) return raw;
    const weekday = new Intl.DateTimeFormat('zh-CN', {timeZone: 'Asia/Shanghai',
      weekday: 'short'}).format(date);
    return raw.slice(5, 7).replace(/^0/, '') + '月' +
      raw.slice(8, 10).replace(/^0/, '') + '日 ' + weekday;
  }

  function addDays(isoDate, days) {
    if (!DATE_RE.test(text(isoDate, 10))) return '';
    const date = new Date(text(isoDate, 10) + 'T12:00:00+08:00');
    date.setUTCDate(date.getUTCDate() + Number(days || 0));
    return todayIso(date);
  }

  function isSchoolHttps(value) {
    if (typeof value !== 'string' || !value.trim()) return false;
    let parsed;
    try { parsed = new URL(value.trim()); } catch (error) { return false; }
    if (parsed.protocol !== 'https:') return false;
    if (parsed.username || parsed.password) return false;
    if (parsed.port && parsed.port !== '443') return false;
    const host = (parsed.hostname || '').toLowerCase();
    if (host !== 'tsinghua.edu.cn' && !host.endsWith('.tsinghua.edu.cn')) return false;
    const location = (parsed.pathname || '') + '?' + (parsed.search || '');
    if (/(?:^|\/)(?:cas|sso|login|auth|oauth|idp)(?:\/|$)/i.test(parsed.pathname || '')) return false;
    if (/(?:^|[?&])(?:service|ticket|client_id|redirect_uri)=/i.test(location)) return false;
    const params = new URLSearchParams(parsed.search || '');
    for (const key of params.keys()) {
      if (/^(?:service|ticket|client_id|redirect_uri)$/i.test(key)) return false;
    }
    return true;
  }

  function linkFor(value) {
    return isSchoolHttps(value) ? text(value, 2000) : '';
  }

  function stateInfo(display) {
    const key = display && typeof display.state === 'string' && STATES[display.state]
      ? display.state : 'UNKNOWN';
    const base = STATES[key];
    return {
      state: key,
      tone: base.tone,
      label: base.label,
      message: text(display && display.message, 300) || base.message,
      nextStep: text(display && display.next_step, 300) || base.next
    };
  }

  function freshnessInfo(level) {
    const key = typeof level === 'string' ? level : '';
    if (key === 'LIVE_BUSINESS_READ' || key === 'LIVE_PARTIAL_DIRECTORY') {
      return {code: 'LIVE', label: '实时读取', detail: '本次从校园来源实时读取'};
    }
    if (key === 'REMOTE_RECORD_VERIFIED') {
      return {code: 'VERIFIED', label: '远程记录已核对', detail: '本次只核对远程记录'};
    }
    if (key === 'DIRECTORY_CACHE') {
      return {code: 'CACHE', label: '历史缓存', detail: '结果来自历史缓存，不是本次实时读取'};
    }
    if (key === 'LOCAL_EXECUTION_STATE') {
      return {code: 'LOCAL', label: '本地状态', detail: '结果由本地执行状态汇总'};
    }
    if (['BUSINESS_READ', 'PAGE_READ', 'AUTHENTICATED_PAGE', 'PUBLIC_PAGE',
         'DIRECTORY_READ', 'TRANSPORT_READ'].indexOf(key) >= 0) {
      return {code: 'SNAPSHOT', label: '读取快照', detail: '结果是本次读取的快照'};
    }
    if (['NONE', 'OFFLINE_CONTRACT', 'SYNTHETIC_TRANSPORT', 'SEE_OPERATION_EVIDENCE'].indexOf(key) >= 0) {
      return {code: 'UNKNOWN', label: '未标注实时性', detail: '来源没有声明实时或历史状态'};
    }
    return {code: 'UNKNOWN', label: '未标注实时性', detail: '来源没有声明实时或历史状态'};
  }

  function completenessInfo(display, envelope) {
    const block = display && typeof display.completeness === 'object' && display.completeness
      ? display.completeness : {};
    const sourceComplete = block.source_complete === true;
    const snapshotComplete = block.snapshot_complete === true;
    const complete = block.complete === true;
    let label;
    if (!sourceComplete) label = '来源未完整读取';
    else if (!snapshotComplete) label = '快照不完整';
    else if (!complete) label = '范围未确认完整';
    else label = '范围完整';
    const details = [label];
    const missing = Array.isArray(envelope && envelope.missing_pages)
      ? envelope.missing_pages.filter(function (page) { return Number.isInteger(page) && page > 0; })
      : [];
    if (missing.length) details.push('缺少第 ' + missing.join('、') + ' 页');
    if (envelope && envelope.next_cursor) details.push('还有更多页');
    return {label: details.join('；'), complete: complete,
            sourceComplete: sourceComplete, snapshotComplete: snapshotComplete,
            missingPages: missing, hasMore: Boolean(envelope && envelope.next_cursor)};
  }

  function envelopeModel(value) {
    const envelope = value && typeof value === 'object' ? value : {};
    const display = envelope.display && typeof envelope.display === 'object'
      ? envelope.display : {};
    const info = stateInfo(display);
    const provenance = display.provenance && typeof display.provenance === 'object'
      ? display.provenance : {};
    const source = envelope.source && typeof envelope.source === 'object'
      ? envelope.source : {};
    const paging = display.paging && typeof display.paging === 'object'
      ? display.paging : {};
    const checked = text(provenance.checked_at || envelope.checked_at, 40);
    return {
      status: text(envelope.status, 40) || 'UNKNOWN',
      state: info.state, tone: info.tone, label: info.label,
      message: info.message, nextStep: info.nextStep,
      checkedAt: checked, checkedLabel: formatTimestamp(checked),
      freshness: freshnessInfo(provenance.verification_level || envelope.verification_level),
      completeness: completenessInfo(display, envelope),
      paging: {
        returned: intOrNull(paging.returned_items),
        total: intOrNull(paging.total_items),
        hasMore: paging.has_more === true || Boolean(envelope.next_cursor)
      },
      source: {
        serviceId: text(provenance.service_id || source.service_id, 80),
        operation: text(provenance.operation || source.operation, 120),
        adapterVersion: text(provenance.adapter_version || source.adapter_version, 80),
        url: linkFor(provenance.url || source.url)
      },
      requestId: text(envelope.request_id, 80),
      warnings: asList(envelope.warnings),
      items: Array.isArray(envelope.items) ? envelope.items : []
    };
  }

  function statusLabel(value) {
    const key = text(value, 40).toUpperCase();
    return STATUS_LABELS[key] || text(value, 40) || '状态未知';
  }

  function formatBytes(value) {
    if (!Number.isInteger(value) || value < 0) return '大小未知';
    if (value < 1024) return value + ' B';
    const units = ['KB', 'MB', 'GB'];
    let size = value / 1024;
    let index = 0;
    while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
    return (size >= 10 ? size.toFixed(0) : size.toFixed(1)) + ' ' + units[index];
  }

  function semesterOptions(envelope) {
    const items = envelope && Array.isArray(envelope.items) ? envelope.items : [];
    const seen = new Set();
    const options = [];
    items.forEach(function (row) {
      if (!row || typeof row !== 'object') return;
      const id = text(row.semester_id || row.id || row.semester, 40);
      if (!id || seen.has(id)) return;
      seen.add(id);
      options.push({
        id: id,
        label: text(row.title || row.name || id, 120),
        current: row.current === true,
        startsOn: text(row.starts_on, 10),
        endsOn: text(row.ends_on, 10)
      });
    });
    return options;
  }

  function courseKind(row) {
    const key = text(row && (row.role || row.type || row.kind), 40).toLowerCase();
    if (key === 'ta') return 'assistant';
    return KIND_LABELS[key] ? key : 'other';
  }

  function filterCourses(items, filters) {
    const f = filters || {};
    const query = normalise(f.query);
    return (Array.isArray(items) ? items : []).filter(function (row) {
      if (!row || typeof row !== 'object') return false;
      if (f.semester && text(row.semester, 80) !== f.semester) return false;
      if (f.kind && courseKind(row) !== f.kind) return false;
      if (query) {
        const teachers = Array.isArray(row.teacher_names) ? row.teacher_names.join(' ') : '';
        const haystack = normalise([row.course_name, row.course_id, row.teacher_name,
                                    row.teacherName, teachers].join(' '));
        if (haystack.indexOf(query) < 0) return false;
      }
      return true;
    });
  }

  function courseFacets(items) {
    const kinds = new Set();
    (Array.isArray(items) ? items : []).forEach(function (row) {
      if (row && typeof row === 'object') kinds.add(courseKind(row));
    });
    return Array.from(kinds).map(function (key) {
      return {value: key, label: KIND_LABELS[key] || '其他'};
    });
  }

  function resourceType(row) {
    const item = row && typeof row === 'object' ? row : {};
    const mime = text(item.mime_type, 120).toLowerCase();
    for (let index = 0; index < TYPE_BY_MIME.length; index += 1) {
      if (mime.indexOf(TYPE_BY_MIME[index][0]) === 0) return TYPE_BY_MIME[index][1];
    }
    if (mime.indexOf('image/') === 0) return '图片';
    if (mime.indexOf('video/') === 0) return '视频';
    if (mime.indexOf('audio/') === 0) return '音频';
    const match = text(item.title, 400).match(/\.([A-Za-z0-9]{1,10})$/);
    if (match) {
      const kind = TYPE_BY_EXT[match[1].toLowerCase()];
      if (kind) return kind;
    }
    const declared = text(item.kind, 40);
    return RESOURCE_KINDS.indexOf(declared) >= 0 ? declared : '其他';
  }

  function filterResources(items, filters) {
    const f = filters || {};
    const query = normalise(f.query);
    return (Array.isArray(items) ? items : []).filter(function (row) {
      if (!row || typeof row !== 'object') return false;
      if (f.semester && text(row.semester, 80) !== f.semester) return false;
      if (f.courseId && text(row.course_id, 128) !== f.courseId) return false;
      if (f.kind && resourceType(row) !== f.kind) return false;
      if (query) {
        const haystack = normalise([row.title, row.course_name, row.course_id,
                                    row.stable_resource_uid].join(' '));
        if (haystack.indexOf(query) < 0) return false;
      }
      return true;
    });
  }

  function resourceFacets(items) {
    const kinds = new Set();
    const courses = new Map();
    (Array.isArray(items) ? items : []).forEach(function (row) {
      if (!row || typeof row !== 'object') return;
      kinds.add(resourceType(row));
      const id = text(row.course_id, 128);
      if (id && !courses.has(id)) courses.set(id, text(row.course_name || id, 160));
    });
    return {
      kinds: Array.from(kinds).sort().map(function (kind) { return {value: kind, label: kind}; }),
      courses: Array.from(courses.entries()).map(function (pair) {
        return {value: pair[0], label: pair[1]};
      })
    };
  }

  function stableUid(row) {
    if (!row || typeof row !== 'object') return '';
    const value = text(row.stable_resource_uid, 240);
    return value && !/\s/.test(value) ? value : '';
  }

  function resourceSelectable(row) {
    return Boolean(stableUid(row));
  }

  function createSelection() {
    const ids = new Set();
    let snapshot = 0;
    return {
      get snapshot() { return snapshot; },
      get size() { return ids.size; },
      has: function (uid) { return ids.has(uid); },
      add: function (uid) {
        const key = text(uid, 240);
        if (key && !/\s/.test(key)) ids.add(key);
        return ids.size;
      },
      remove: function (uid) { ids.delete(text(uid, 240)); return ids.size; },
      toggle: function (uid) {
        const key = text(uid, 240);
        if (!key) return ids.size;
        if (ids.has(key)) ids.delete(key); else ids.add(key);
        return ids.size;
      },
      clear: function () { ids.clear(); return 0; },
      values: function () { return Array.from(ids).sort(); },
      reset: function () { ids.clear(); snapshot += 1; return snapshot; }
    };
  }

  function nextQuery(current, patch) {
    const filters = Object.assign({}, current || {});
    let changed = false;
    Object.keys(patch || {}).forEach(function (key) {
      if (patch[key] !== filters[key]) changed = true;
      filters[key] = patch[key];
    });
    return {filters: filters, changed: changed};
  }

  function resourceQuery(filters, cursor) {
    const f = filters || {};
    const query = {page_size: PAGE_SIZE};
    if (f.semester) query.semester = f.semester;
    if (f.query) query.query = f.query;
    if (f.courseId) query.course_id = f.courseId;
    if (cursor) query.cursor = cursor;
    return query;
  }

  function selectionPlan(selection, filters, loaded, total) {
    const values = selection && typeof selection.values === 'function' ? selection.values() : [];
    return {
      ids: values,
      count: values.length,
      filters: Object.assign({}, filters || {}),
      loaded: Number.isInteger(loaded) ? loaded : 0,
      total: Number.isInteger(total) ? total : null,
      stable: true,
      note: '导出范围按稳定编号确定；本页不执行下载或导出，请在原有受控工具中完成。'
    };
  }

  function calendarTime(item) {
    const row = item && typeof item === 'object' ? item : {};
    const time = row.time && typeof row.time === 'object' ? row.time : null;
    if (time && time.kind === 'DATE') {
      const date = text(time.date, 10);
      return DATE_RE.test(date) ? {kind: 'DATE', date: date, start: '', end: ''}
        : {kind: 'UNKNOWN', date: '', start: '', end: ''};
    }
    if (time && time.kind === 'DATE_TIME') {
      const start = text(time.start, 40);
      return ISO_RE.test(start)
        ? {kind: 'DATE_TIME', date: start.slice(0, 10), start: start, end: text(time.end, 40)}
        : {kind: 'UNKNOWN', date: '', start: '', end: ''};
    }
    const raw = text(row.start || row.due_at || row.date, 40);
    if (DATE_RE.test(raw)) return {kind: 'DATE', date: raw, start: '', end: ''};
    if (ISO_RE.test(raw)) {
      return {kind: 'DATE_TIME', date: raw.slice(0, 10), start: raw,
              end: text(row.end || row.end_at, 40)};
    }
    return {kind: 'UNKNOWN', date: '', start: '', end: ''};
  }

  function timeText(kind, start, end) {
    if (kind === 'DATE') return '全天';
    if (kind !== 'DATE_TIME') return '时间未知';
    const startText = start.slice(11, 16);
    if (end && ISO_RE.test(end)) {
      const endText = end.slice(11, 16);
      if (end.slice(0, 10) !== start.slice(0, 10)) return startText + ' 起';
      return startText + '–' + endText;
    }
    return startText;
  }

  function calendarItem(row, nowIso) {
    const item = row && typeof row === 'object' ? row : {};
    const when = calendarTime(item);
    const warnings = asList(item.warnings);
    const limitation = warnings.some(function (warning) {
      return CALENDAR_LIMIT_WARNINGS.has(warning);
    });
    const startMs = parseSourceTime(when.start);
    const endMs = parseSourceTime(when.end);
    const nowMs = parseSourceTime(nowIso || new Date().toISOString());
    let overdue = false;
    if (when.kind === 'DATE') overdue = Boolean(when.date && when.date < todayIso(nowIso));
    else if (when.kind === 'DATE_TIME' && Number.isFinite(startMs)) {
      overdue = Number.isFinite(endMs) ? endMs < nowMs : startMs < nowMs;
    }
    const rawStatus = text(item.status || item.business_status, 40).toUpperCase();
    const completed = TERMINAL.has(rawStatus);
    return {
      uid: text(item.event_uid || item.event_id || '', 240),
      title: text(item.title, 240) || '未命名事项',
      when: when,
      kind: when.kind,
      dateKey: when.date,
      dateLabel: when.date ? formatDate(when.date) : '时间未知',
      timeLabel: timeText(when.kind, when.start, when.end),
      semantics: text(item.time_semantics, 40) || (when.kind === 'DATE' ? 'DATE' : 'DATE_TIME'),
      status: statusLabel(rawStatus || item.status),
      statusKey: rawStatus,
      source: text(item.source_id || (item.source && item.source.record_id), 120),
      location: text(item.location, 200),
      overdue: overdue,
      completed: completed,
      valid: when.kind !== 'UNKNOWN' && item.calendar_valid !== false,
      limitation: limitation,
      warnings: warnings,
      sourceUrl: linkFor(item.source_url)
    };
  }

  function calendarModel(envelope, nowIso) {
    const model = envelopeModel(envelope);
    const today = todayIso(nowIso);
    const items = model.items.map(function (row) { return calendarItem(row, nowIso); });
    const buckets = {overdue: [], today: [], upcoming: [], undated: [], completed: []};
    items.forEach(function (item) {
      if (!item.valid) { buckets.undated.push(item); return; }
      if (item.completed) { buckets.completed.push(item); return; }
      if (item.overdue) { buckets.overdue.push(item); return; }
      if (item.dateKey === today) { buckets.today.push(item); return; }
      if (item.kind === 'DATE' && item.dateKey < today) { buckets.overdue.push(item); return; }
      buckets.upcoming.push(item);
    });
    const byUid = new Map(items.map(function (item) { return [item.uid, item]; }));
    const conflicts = (Array.isArray(envelope && envelope.conflicts) ? envelope.conflicts : [])
      .map(function (conflict) {
        const ids = Array.isArray(conflict && conflict.events) ? conflict.events : [];
        return {
          events: ids.map(function (uid) { return text(uid, 240); }),
          titles: ids.map(function (uid) {
            const found = byUid.get(text(uid, 240));
            return found ? found.title : text(uid, 240);
          }),
          start: text(conflict && conflict.start, 40),
          end: text(conflict && conflict.end, 40),
          startLabel: formatTimestamp(conflict && conflict.start),
          endLabel: formatTimestamp(conflict && conflict.end),
          basis: text(conflict && conflict.basis, 60)
        };
      });
    const limitations = [];
    if (items.some(function (item) { return item.limitation; })) {
      limitations.push('部分课程或事项缺少校历、节次表，未推断具体时间；仅保留来源给出的原始周期。');
    }
    const excluded = Array.isArray(envelope && envelope.excluded) ? envelope.excluded : [];
    if (excluded.length) {
      limitations.push(excluded.length + ' 条记录因时间或标识无法确认而未进入日程；不会编造日期。');
    }
    if (model.completeness.hasMore) {
      limitations.push('当前只读取了第一页日程；还有更多页。');
    }
    const order = function (left, right) {
      return String(left.dateKey + (left.when.start || '')).localeCompare(
        String(right.dateKey + (right.when.start || '')));
    };
    Object.keys(buckets).forEach(function (key) { buckets[key].sort(order); });
    return {
      model: model, items: items, buckets: buckets, conflicts: conflicts,
      limitations: limitations,
      counts: {
        total: items.length, overdue: buckets.overdue.length, today: buckets.today.length,
        upcoming: buckets.upcoming.length, undated: buckets.undated.length,
        completed: buckets.completed.length, conflicts: conflicts.length
      }
    };
  }

  function healthModel(envelope) {
    const model = envelopeModel(envelope);
    const data = envelope && envelope.data && typeof envelope.data === 'object'
      ? envelope.data : {};
    const rows = (Array.isArray(data.services) ? data.services : model.items)
      .filter(function (row) { return row && typeof row === 'object'; })
      .map(function (row) {
        const implementation = text(row.implementation_state, 40).toUpperCase() || 'UNKNOWN';
        const runtime = text(row.runtime_availability, 40).toUpperCase() || 'UNKNOWN';
        return {
          serviceId: text(row.service_id, 80),
          status: statusLabel(row.status),
          statusKey: text(row.status, 40).toUpperCase(),
          checkedLabel: formatTimestamp(row.checked_at),
          complete: row.complete === true,
          implementation: implementation,
          implementationLabel: {
            IMPLEMENTED: '已实现', BLOCKED: '来源阻塞', NOT_IMPLEMENTED: '未实现',
            UNKNOWN: '未验证'
          }[implementation] || implementation,
          implementationTone: implementation === 'IMPLEMENTED' ? 'ok'
            : (implementation === 'BLOCKED' ? 'limited' : 'unknown'),
          runtime: runtime,
          runtimeLabel: {
            AVAILABLE: '可用', UNAVAILABLE: '不可用', LIMITED: '受限', UNKNOWN: '未知'
          }[runtime] || runtime,
          evidence: text(row.evidence_level, 40).toUpperCase() || 'NONE',
          evidenceLabel: {
            NONE: '无证据', OFFLINE_CONTRACT: '离线契约', SYNTHETIC_TRANSPORT: '合成传输',
            LIVE_READ: '真实读取', LIVE_WRITE: '真实写入', UNKNOWN: '未知'
          }[text(row.evidence_level, 40).toUpperCase()] || text(row.evidence_level, 40) || '无证据'
        };
      });
    const implemented = rows.filter(function (row) { return row.implementation === 'IMPLEMENTED'; }).length;
    return {
      model: model, rows: rows, warnings: model.warnings,
      counts: {total: rows.length, implemented: implemented,
               blocked: rows.filter(function (row) { return row.implementation === 'BLOCKED'; }).length,
               missing: rows.filter(function (row) { return row.implementation === 'NOT_IMPLEMENTED'; }).length}
    };
  }

  function severityRank(tone) {
    const ranks = {error: 0, auth: 1, limited: 2, partial: 3, unknown: 4, empty: 5, ok: 6};
    const rank = ranks[tone];
    return rank === undefined ? 4 : rank;
  }

  function attentionSummary(models) {
    const list = (Array.isArray(models) ? models : []).filter(Boolean);
    const attention = list.filter(function (model) { return model.state !== 'READY'; });
    attention.sort(function (left, right) {
      return severityRank(left.tone) - severityRank(right.tone);
    });
    return {
      total: list.length,
      ready: list.filter(function (model) { return model.state === 'READY'; }).length,
      attention: attention.length,
      headline: attention.length
        ? attention.length + ' 处需要留意：' + attention.map(function (model) {
          return model.label;
        }).join('、')
        : '全部卡片已按当前范围读取。'
    };
  }

  return {
    VERSION: VERSION, API_BASE: API_BASE, API: API, PAGE_SIZE: PAGE_SIZE,
    REFRESH_MIN_SECONDS: REFRESH_MIN_SECONDS, PAGE_IDS: PAGE_IDS, PAGES: PAGES,
    OFFICIAL_ENTRIES: OFFICIAL_ENTRIES, STATES: STATES, STATUS_LABELS: STATUS_LABELS,
    KIND_LABELS: KIND_LABELS, RESOURCE_KINDS: RESOURCE_KINDS,
    text: text, asList: asList, todayIso: todayIso, formatTimestamp: formatTimestamp,
    formatDate: formatDate, addDays: addDays, isSchoolHttps: isSchoolHttps,
    parseSourceTime: parseSourceTime,
    linkFor: linkFor, stateInfo: stateInfo, freshnessInfo: freshnessInfo,
    completenessInfo: completenessInfo, envelopeModel: envelopeModel,
    statusLabel: statusLabel, formatBytes: formatBytes,
    semesterOptions: semesterOptions, courseKind: courseKind,
    filterCourses: filterCourses, courseFacets: courseFacets,
    resourceType: resourceType, filterResources: filterResources,
    resourceFacets: resourceFacets, stableUid: stableUid,
    resourceSelectable: resourceSelectable, createSelection: createSelection,
    nextQuery: nextQuery, resourceQuery: resourceQuery, selectionPlan: selectionPlan,
    calendarTime: calendarTime, calendarItem: calendarItem, calendarModel: calendarModel,
    healthModel: healthModel, attentionSummary: attentionSummary
  };
}));
