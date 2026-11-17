'use strict';
/* Openthu local read-only dashboard (module 7.1).
 *
 * Every value that reaches the DOM goes through textContent; no innerHTML,
 * outerHTML or insertAdjacentHTML exists in this file.  The only network calls
 * are GET requests to the frozen local-read-api-v1 routes, issued by api.js.
 * There is no write, booking, submission, payment, model or key entry point.
 *
 * State is deliberately in memory only: no localStorage, sessionStorage,
 * IndexedDB or service worker.  When the local session expires the page wipes
 * every cached card and selection before showing a session notice, so a new
 * session can never display the previous session's details.
 */
(function () {
  const M = window.OpenthuModel;
  const API = window.OpenthuApi;
  const root = document.documentElement;
  if (!M || !API) {
    root.dataset.app = 'failed';
    return;
  }
  const client = API.createApi({base: ''});

  const dom = {
    nav: document.getElementById('nav-list'),
    title: document.getElementById('page-title'),
    note: document.getElementById('page-note'),
    live: document.getElementById('live-status'),
    view: document.getElementById('view'),
    banner: document.getElementById('session-banner')
  };

  const NOT_ENABLED_CODES = new Set([
    'LOCAL_API_FACADE_UNAVAILABLE', 'ADAPTER_REQUIRED', 'FACADE_CAPABILITY_UNAVAILABLE',
    'CAPABILITY_UNAVAILABLE', 'NOT_IMPLEMENTED', 'LOCAL_DASHBOARD_UNAVAILABLE'
  ]);
  const WARNING_LABELS = {
    LOCAL_API_LINK_WITHHELD: '部分来源链接不符合校内 HTTPS 规则，已隐藏。',
    LOCAL_API_TEXT_TRUNCATED: '文本过长，已截断显示。',
    LOCAL_API_CREDENTIAL_REDACTED: '凭据形态内容已隐藏。',
    LOCAL_API_ABSOLUTE_PATH_REDACTED: '绝对路径已隐藏。',
    MISSING_SOURCE_PAGES: '来源缺少部分页。',
    SOURCE_AUTH_REQUIRED: '来源需要认证。',
    SOURCE_TIMEOUT: '来源读取超时。',
    SEMESTER_REQUIRED: '需要选择学期。',
    PERIOD_TABLE_MISSING: '缺少校历或节次表。',
    TIME_NOT_FROM_PERIOD_TABLE: '时间不是来自校历或节次表。',
    CALENDAR_PERIOD_TABLE_SEMESTER_MISMATCH: '校历与节次表的学期不一致。',
    REPEATED_UPSTREAM_PAGE: '来源返回了重复页。',
    UPSTREAM_PAGE_FAILED: '来源页读取失败。',
    READ_CANCELLED: '读取已取消。'
  };
  const OVERVIEW_WINDOW_DAYS = 14;

  const state = {
    route: 'overview',
    routed: false,
    session: 'unknown',
    cache: new Map(),
    semesters: {loaded: false, loading: null, options: [], manual: false, error: null},
    data: {
      overview: {},
      courses: null,
      resources: null,
      calendar: null,
      library: {},
      health: null,
      services: null
    },
    filters: {
      courses: {semester: '', query: '', kind: ''},
      resources: {semester: '', courseId: '', kind: '', query: ''},
      calendar: {startDate: '', endDate: '', mode: 'timeline'}
    },
    resources: {
      items: [], cursor: null, cursorFilters: null, loaded: 0, total: null,
      selection: M.createSelection(), titles: new Map(), loading: false, snapshot: 0,
      error: null
    },
    pending: new Map()
  };

  // ---------------------------------------------------------------- helpers

  function append(node, child) {
    if (child === null || child === undefined || child === false) return;
    if (Array.isArray(child)) {
      child.forEach(function (item) { append(node, item); });
      return;
    }
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }

  function h(tag, props) {
    const node = document.createElement(tag);
    const attributes = props || {};
    Object.keys(attributes).forEach(function (key) {
      const value = attributes[key];
      if (value === null || value === undefined || value === false) return;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = String(value);
      else if (key === 'dataset') {
        Object.keys(value).forEach(function (name) { node.dataset[name] = String(value[name]); });
      } else if (key === 'on') {
        Object.keys(value).forEach(function (name) {
          node.addEventListener(name, value[name]);
        });
      } else if (value === true) node.setAttribute(key, '');
      else node.setAttribute(key, String(value));
    });
    for (let index = 2; index < arguments.length; index += 1) append(node, arguments[index]);
    return node;
  }

  function replace(node, child) {
    node.replaceChildren();
    append(node, child);
  }

  function cachedNote() {
    return h('p', {class: 'item-meta', dataset: {cached: 'true'},
                   text: '10 秒内复用上次读取结果；点刷新可重新读取。'});
  }

  function announce(message) {
    dom.live.textContent = message || '';
  }

  function localModel(stateName, message, nextStep, extra) {
    const value = extra || {};
    return M.envelopeModel({
      status: value.status || 'UNKNOWN',
      request_id: value.requestId || '',
      warnings: value.warnings || [],
      items: value.items || [],
      next_cursor: value.nextCursor || null,
      display: {
        state: stateName,
        message: message,
        next_step: nextStep,
        completeness: {source_complete: false, snapshot_complete: false, complete: false},
        provenance: {service_id: value.serviceId || '', operation: value.operation || '',
                     adapter_version: '', checked_at: '', verification_level: 'NONE', url: null},
        paging: {returned_items: null, total_items: null, has_more: false}
      }
    });
  }

  function networkModel(requestId) {
    return localModel('ERROR', '无法连接本地看板服务。',
      '确认看板进程仍在运行，然后重试；JSON API 可能仍然可用。', {requestId: requestId});
  }

  function invalidJsonModel(requestId) {
    return localModel('ERROR', '本地看板返回了无法识别的结果。',
      '这是本地契约问题；请记录请求编号并稍后重试。', {requestId: requestId});
  }

  function withNotEnabled(model, body) {
    const code = body && body.error && typeof body.error.code === 'string'
      ? body.error.code : '';
    if (!NOT_ENABLED_CODES.has(code)) return model;
    const base = M.STATES.NOT_ENABLED;
    return Object.assign({}, model, {
      state: 'NOT_ENABLED', tone: base.tone, label: base.label,
      message: model.message || base.message,
      nextStep: model.nextStep || base.next
    });
  }

  function badge(model) {
    return h('span', {class: 'badge badge-' + model.tone,
                      dataset: {state: model.state}}, model.label);
  }

  function metaList(model) {
    const rows = [
      ['来源时间', model.checkedLabel],
      ['实时性', model.freshness.label],
      ['完整性', model.completeness.label],
      ['来源', [model.source.serviceId, model.source.operation].filter(Boolean).join(' / ') || '本地']
    ];
    const list = h('dl', {class: 'meta'});
    rows.forEach(function (row) {
      append(list, h('div', {}, h('dt', {text: row[0]}), h('dd', {text: row[1]})));
    });
    return list;
  }

  function stateBlock(model) {
    const block = h('div', {class: 'state-block'});
    append(block, h('p', {class: 'state-message', text: model.message}));
    const next = model.nextStep && model.nextStep !== '无' ? model.nextStep : '无需操作';
    append(block, h('p', {class: 'next-step', text: '下一步：' + next}));
    if (model.requestId) {
      append(block, h('p', {class: 'request-id', text: '请求编号：' + model.requestId}));
    }
    if (model.warnings.length) {
      const labels = [];
      model.warnings.forEach(function (warning) {
        if (warning.indexOf('LOCAL_API_LINK_WITHHELD_AT:') === 0) return;
        const label = WARNING_LABELS[warning] || '来源告警（安全分类）';
        if (labels.indexOf(label) < 0) labels.push(label);
      });
      if (labels.length) append(block, h('p', {class: 'item-note', text: '提示：' + labels.join(' ')}));
    }
    return block;
  }

  function loadingBlock(message, onCancel, cancelKey) {
    const button = h('button', {type: 'button', dataset: {action: 'cancel', key: cancelKey}},
      '取消加载');
    button.addEventListener('click', onCancel);
    return h('div', {class: 'loading-state'},
      h('span', {class: 'spinner', 'aria-hidden': 'true'}),
      h('span', {text: message || '正在读取…'}), button);
  }

  function emptyBlock(message) {
    return h('p', {class: 'empty-state', text: message});
  }

  function actionButton(label, action, key, kind) {
    const attrs = {type: 'button', class: kind || '', dataset: {action: action}};
    if (key) attrs.dataset.key = key;
    return h('button', attrs, label);
  }

  function pageLink(label, pageId, extra) {
    const attrs = {href: '#/' + pageId};
    if (extra) Object.assign(attrs, extra);
    return h('a', attrs, label);
  }

  function pageInfo(id) {
    for (let index = 0; index < M.PAGES.length; index += 1) {
      if (M.PAGES[index].id === id) return M.PAGES[index];
    }
    return M.PAGES[0];
  }

  function safeLink(label, url) {
    const target = M.linkFor(url);
    if (!target) {
      return h('span', {class: 'item-note', dataset: {linkWithheld: 'true'},
                        text: '链接已隐藏（非校内 HTTPS 或含认证参数）'});
    }
    return h('a', {href: target, target: '_blank', rel: 'noopener noreferrer',
                   referrerpolicy: 'no-referrer', dataset: {officialLink: 'true'}}, label);
  }

  // ---------------------------------------------------------------- routing

  function pageFromHash() {
    const raw = location.hash.replace(/^#\/?/, '');
    return M.PAGE_IDS.indexOf(raw) >= 0 ? raw : 'overview';
  }

  function renderNav() {
    const items = M.PAGES.map(function (page) {
      return h('li', {}, h('a', {href: '#/' + page.id,
                                 dataset: {page: page.id}}, page.label));
    });
    replace(dom.nav, items);
  }

  function updateNav(id) {
    Array.from(dom.nav.querySelectorAll('a[data-page]')).forEach(function (link) {
      if (link.dataset.page === id) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    });
  }

  async function route() {
    const next = pageFromHash();
    const changed = state.routed && next !== state.route;
    const first = !state.routed;
    state.route = next;
    state.routed = true;
    updateNav(next);
    const page = pageInfo(next);
    dom.title.textContent = page.title;
    dom.note.textContent = PAGE_NOTES[next] || '';
    document.title = page.title + ' · Openthu 本地看板';
    replace(dom.view, '');
    announce('正在打开' + page.label + '…');
    if (!first) {
      const alive = await probeSession();
      if (!alive || state.route !== next) return;
    }
    renderPage(next);
    if (changed) dom.title.focus();
    root.dataset.app = 'ready';
    root.dataset.route = next;
    dom.view.dataset.view = next;
  }

  async function probeSession() {
    const result = await client.get(M.API.health, {}, {});
    if (result.httpStatus === 401) {
      sessionLost('expired');
      return false;
    }
    if (result.networkError) {
      sessionLost('unreachable');
      return false;
    }
    return true;
  }

  const PAGE_NOTES = {
    overview: '课程、截止日期、借阅、预约和资料一屏总览；每张卡片独立读取，互不遮挡。成绩与写入操作不在本页。',
    courses: '来自网络学堂的课程读取；筛选只在本地显示，不改变来源数据。',
    resources: '课程资料索引按学期读取；选择与导出范围按稳定编号记录，本页不执行下载。',
    calendar: '日期级、精确时刻、逾期和冲突分别标注；缺少校历或节次表时不推断具体时间。',
    library: '只显示既有借阅和预约记录；预约、取消、付款和续借请使用原有受控工具。',
    health: '本地能力、运行可用性和来源证据；健康报告不探测真实站点，本地读取不等于校园可用。'
  };

  function renderPage(id) {
    if (state.session === 'lost') {
      renderSessionLostView();
      return;
    }
    if (id === 'overview') renderOverviewPage();
    else if (id === 'courses') renderCoursesPage();
    else if (id === 'resources') renderResourcesPage();
    else if (id === 'calendar') renderCalendarPage();
    else if (id === 'library') renderLibraryPage();
    else renderHealthPage();
  }

  // ---------------------------------------------------------------- requests

  function abortKey(key, silent) {
    const controller = state.pending.get(key);
    if (controller) {
      controller.abort();
      state.pending.delete(key);
      syncBusy();
      if (!silent) announce('已取消本次读取。');
    }
  }

  function abortAll(silent) {
    Array.from(state.pending.keys()).forEach(function (key) { abortKey(key, true); });
    syncBusy();
    if (!silent) announce('已取消全部进行中的读取。');
  }

  function syncBusy() {
    const busy = state.pending.size > 0;
    Array.from(document.querySelectorAll('[data-action^="refresh-"]')).forEach(function (button) {
      button.disabled = busy;
    });
  }

  function cacheSignature(path, params) {
    const keys = Object.keys(params || {}).sort();
    return path + '?' + keys.map(function (key) {
      return key + '=' + String(params[key]);
    }).join('&');
  }

  async function request(key, path, params, options) {
    const opts = options || {};
    const signature = cacheSignature(path, params);
    if (opts.cache !== false) {
      const hit = state.cache.get(signature);
      if (hit && Date.now() - hit.at < M.REFRESH_MIN_SECONDS * 1000) {
        return {kind: 'ok', body: hit.body, cached: true,
                model: withNotEnabled(M.envelopeModel(hit.body), hit.body)};
      }
    }
    abortKey(key, true);
    const controller = new AbortController();
    state.pending.set(key, controller);
    syncBusy();
    announce('正在读取' + (opts.label ? '：' + opts.label : '') + '…');
    const result = await client.get(path, params || {}, {signal: controller.signal});
    if (state.pending.get(key) === controller) state.pending.delete(key);
    syncBusy();
    if (result.aborted) return {kind: 'cancelled'};
    if (result.networkError === 'LOCAL_API_NETWORK_UNAVAILABLE') {
      return {kind: 'ok', body: null, model: networkModel(result.requestId)};
    }
    if (result.networkError === 'LOCAL_API_INVALID_JSON') {
      return {kind: 'ok', body: null, model: invalidJsonModel(result.requestId)};
    }
    const body = result.body || {};
    const display = body.display && typeof body.display === 'object' ? body.display : {};
    if (result.httpStatus === 401 || display.state === 'LOCAL_SESSION_REQUIRED') {
      sessionLost();
      return {kind: 'session'};
    }
    let model = M.envelopeModel(body);
    model = withNotEnabled(model, body);
    if (result.ok && body && body.status) state.cache.set(signature, {body: body, at: Date.now()});
    return {kind: 'ok', body: body, model: model};
  }

  function localTimeWindow() {
    const today = M.todayIso(new Date());
    return {start: M.addDays(today, -7), end: M.addDays(today, OVERVIEW_WINDOW_DAYS)};
  }

  // ---------------------------------------------------------------- semesters

  function loadSemesters() {
    if (state.semesters.loaded) return Promise.resolve(state.semesters.options);
    if (state.semesters.loading) return state.semesters.loading;
    state.semesters.loading = request('semesters', M.API.query + 'learn/semesters',
      {parameters: '{}'}, {label: '学期清单'}).then(function (result) {
        state.semesters.loading = null;
        if (result.kind !== 'ok') return state.semesters.options;
        state.semesters.loaded = true;
        state.semesters.error = result.model.state === 'READY' || result.model.state === 'EMPTY'
          ? null : result.model;
        state.semesters.options = M.semesterOptions(result.body);
        state.semesters.manual = state.semesters.options.length === 0;
        if (state.semesters.options.length && !state.filters.courses.semester
            && !state.filters.resources.semester) {
          const current = state.semesters.options.filter(function (row) { return row.current; })[0]
            || state.semesters.options[0];
          if (current) {
            state.filters.courses.semester = current.id;
            state.filters.resources.semester = current.id;
          }
        }
        if (state.route === 'overview' && state.semesters.options.length) {
          ['courses', 'resources'].forEach(function (key) {
            const model = state.data.overview[key];
            if (model && model.state === 'NEEDS_INPUT') refreshCard(key);
          });
        }
        return state.semesters.options;
      });
    return state.semesters.loading;
  }

  function semesterOptionsMarkup(selected, placeholder) {
    const options = [h('option', {value: ''}, placeholder || '全部学期')];
    state.semesters.options.forEach(function (row) {
      options.push(h('option', {value: row.id, selected: row.id === selected},
        row.label + (row.current ? '（当前）' : '')));
    });
    return options;
  }

  function semesterField(selected, placeholder) {
    const field = h('div', {class: 'field'});
    if (state.semesters.options.length) {
      append(field, h('label', {for: 'filter-semester'}, '学期'));
      append(field, h('select', {id: 'filter-semester', dataset: {filter: 'semester'}},
        semesterOptionsMarkup(selected, placeholder)));
    } else {
      append(field, h('label', {for: 'filter-semester'}, '学期编号'));
      append(field, h('input', {id: 'filter-semester', type: 'text',
        dataset: {filter: 'semester'}, value: selected,
        placeholder: '如 2026-2027-1', autocomplete: 'off'}));
    }
    if (state.semesters.error && !state.semesters.options.length) {
      append(field, h('p', {class: 'item-note',
        text: '学期清单不可用：' + state.semesters.error.message + ' 可手动填写学期编号。'}));
    }
    return field;
  }

  function readFilters(form) {
    const values = {};
    Array.from(form.querySelectorAll('[data-filter]')).forEach(function (field) {
      values[field.dataset.filter] = field.value.trim();
    });
    return values;
  }

  // ---------------------------------------------------------------- overview

  const OVERVIEW_CARDS = [
    {key: 'health', title: '来源健康', page: 'health'},
    {key: 'todos', title: '待办与截止', page: 'calendar'},
    {key: 'courses', title: '课程', page: 'courses'},
    {key: 'resources', title: '课程资料', page: 'resources'},
    {key: 'loans', title: '借阅', page: 'library'},
    {key: 'reservations', title: '图书馆预约', page: 'library'},
    {key: 'orders', title: '体育预约', page: 'library'}
  ];

  function renderOverviewPage() {
    const summary = h('p', {class: 'section-note', dataset: {overviewSummary: 'true'}},
      '正在读取总览…');
    const grid = h('div', {class: 'cards'});
    const note = h('div', {class: 'limitations'},
      h('p', {}, '成绩、密钥、写入和模型生成不在看板中提供；需要时请使用原有受控工具。'),
      h('p', {}, '刷新由你主动触发；每次读取的实时性和完整性标在卡片上。'));
    const cancels = h('div', {class: 'button-row'},
      actionButton('取消全部读取', 'cancel-all', '', 'ghost'));
    replace(dom.view, [summary, grid, cancels, note]);
    OVERVIEW_CARDS.forEach(function (spec) {
      const view = cardShell(spec.key, spec.title, spec.page);
      grid.append(view.article);
      loadOverviewCard(spec, view);
    });
  }

  function cardShell(key, title, pageId) {
    const head = h('div', {class: 'card-head'},
      h('h3', {class: 'card-title', text: title}),
      h('span', {class: 'badge badge-unknown', dataset: {state: 'IDLE'}}, '等待读取'));
    const meta = h('dl', {class: 'meta'});
    const body = h('div');
    const extra = h('div');
    const actions = h('div', {class: 'button-row'});
    const article = h('article', {class: 'card', dataset: {card: key, cardState: 'IDLE'}},
      head, meta, body, extra, actions);
    return {key: key, title: title, page: pageId, article: article, head: head,
            meta: meta, body: body, extra: extra, actions: actions};
  }

  function paintCard(view, model, options) {
    const opts = options || {};
    view.article.dataset.cardState = model.state;
    const badgeSlot = view.head.querySelector('[data-state]');
    if (badgeSlot) badgeSlot.replaceWith(badge(model));
    replace(view.meta, metaList(model));
    replace(view.body, [stateBlock(model), opts.items || '']);
    replace(view.extra, opts.cached ? cachedNote() : '');
    const actions = [];
    if (opts.cancelKey) {
      actions.push(actionButton('取消加载', 'cancel', opts.cancelKey, 'ghost'));
    } else {
      actions.push(actionButton(opts.retryLabel || '刷新', 'refresh-card', view.key));
    }
    if (opts.link) {
      actions.push(pageLink(opts.link.label, opts.link.page));
    } else if (view.page !== state.route) {
      actions.push(pageLink('查看详情', view.page, {dataset: {cardLink: view.key}}));
    }
    replace(view.actions, actions);
  }

  function setCardLoading(view, requestKey) {
    view.article.dataset.cardState = 'LOADING';
    const badgeSlot = view.head.querySelector('[data-state]');
    if (badgeSlot) badgeSlot.replaceWith(h('span', {class: 'badge badge-unknown',
      dataset: {state: 'LOADING'}}, '读取中'));
    replace(view.meta, '');
    replace(view.body, loadingBlock('正在读取…', function () { abortKey(requestKey); },
      requestKey));
    replace(view.actions, '');
  }

  function overviewPreview(key, model) {
    const items = model.items;
    if (key === 'health') {
      const data = model.rawHealth || {};
      const rows = Array.isArray(data.services) ? data.services : [];
      const implemented = rows.filter(function (row) {
        return row && row.implementation_state === 'IMPLEMENTED';
      }).length;
      return h('div', {class: 'item-list'},
        h('p', {class: 'item', text: '服务 ' + rows.length + ' 个；已实现 ' + implemented + ' 个。'}));
    }
    if (!items.length) return emptyBlock(model.state === 'EMPTY' ? '没有记录。' : '暂无可显示条目。');
    const preview = items.slice(0, 3).map(function (row) {
      if (key === 'todos') {
        return h('div', {class: 'item'},
          h('p', {class: 'item-title', text: M.text(row.title, 160) || '未命名事项'}),
          h('p', {class: 'item-meta', text: '截止：' + M.formatTimestamp(row.due_at || row.start_at) +
            ' · ' + M.statusLabel(row.status)}));
      }
      if (key === 'courses') {
        return h('div', {class: 'item'},
          h('p', {class: 'item-title', text: M.text(row.course_name, 160) || '未命名课程'}),
          h('p', {class: 'item-meta', text: [M.text(row.course_id, 80),
            M.text(row.semester, 40), M.KIND_LABELS[M.courseKind(row)]].filter(Boolean).join(' · ')}));
      }
      if (key === 'resources') {
        return h('div', {class: 'item'},
          h('p', {class: 'item-title', text: M.text(row.title, 160) || '未命名资料'}),
          h('p', {class: 'item-meta', text: [M.resourceType(row), M.text(row.course_name, 120)]
            .filter(Boolean).join(' · ')}));
      }
      const when = M.text(row.due_at || row.start || row.start_at || row.date, 40);
      return h('div', {class: 'item'},
        h('p', {class: 'item-title',
          text: M.text(row.title || row.book_title || row.name || row.room_name ||
            row.scene_name || row.venue, 160) || '未命名记录'}),
        h('p', {class: 'item-meta', text: [when ? M.formatTimestamp(when) : '',
          M.statusLabel(row.status)].filter(Boolean).join(' · ')}));
    });
    return h('div', {class: 'item-list'}, preview,
      items.length > preview.length
        ? h('p', {class: 'item-meta', text: '另有 ' + (items.length - preview.length) + ' 条，见详情页。'})
        : '');
  }

  function overviewRequest(spec) {
    const window = localTimeWindow();
    if (spec.key === 'health') return {path: M.API.health, params: {}};
    if (spec.key === 'todos') {
      return {path: M.API.todos, params: {start_date: window.start, end_date: window.end,
                                          page_size: M.PAGE_SIZE}};
    }
    if (spec.key === 'courses') {
      if (!state.filters.courses.semester) return null;
      return {path: M.API.query + 'learn/courses',
              params: {parameters: JSON.stringify({semester: state.filters.courses.semester})}};
    }
    if (spec.key === 'resources') {
      if (!state.filters.resources.semester) return null;
      return {path: M.API.resources,
              params: {semester: state.filters.resources.semester, page_size: M.PAGE_SIZE}};
    }
    if (spec.key === 'loans') return {path: M.API.query + 'library/loans', params: {parameters: '{}'}};
    if (spec.key === 'reservations') {
      return {path: M.API.query + 'library/reservations', params: {parameters: '{}'}};
    }
    return {path: M.API.query + 'sports/orders', params: {parameters: '{}'}};
  }

  async function loadOverviewCard(spec, view, force) {
    const key = 'overview-' + spec.key;
    setCardLoading(view, key);
    const requestSpec = overviewRequest(spec);
    if (!requestSpec) {
      const model = localModel('NEEDS_INPUT', '需要先选择学期。',
        '打开' + (spec.key === 'courses' ? '课程' : '资料') + '页选择学期后重试。',
        {serviceId: 'local', operation: spec.key});
      state.data.overview[spec.key] = model;
      paintCard(view, model, {retryLabel: '重试',
        items: h('p', {class: 'item-meta', text: '未读取来源，不把缺少输入当成空结果。'}),
        link: {label: spec.key === 'courses' ? '去课程页选择学期' : '去资料页选择学期',
               page: spec.key === 'courses' ? 'courses' : 'resources'}});
      updateOverviewSummary();
      return;
    }
    const result = await request(key, requestSpec.path, requestSpec.params,
      {label: spec.title, cache: force !== true});
    if (result.kind === 'cancelled') {
      view.article.dataset.cardState = 'CANCELLED';
      const badgeSlot = view.head.querySelector('[data-state]');
      if (badgeSlot) badgeSlot.replaceWith(h('span', {class: 'badge badge-limited',
        dataset: {state: 'CANCELLED'}}, '已取消'));
      replace(view.body, h('div', {class: 'loading-state'},
        h('span', {text: '本次读取已取消；其他卡片不受影响。'}),
        actionButton('重试', 'refresh-card', view.key)));
      replace(view.actions, '');
      state.data.overview[spec.key] = localModel('LIMITED', '本次读取已取消。',
        '需要时重新读取。', {serviceId: 'local', operation: spec.key});
      updateOverviewSummary();
      return;
    }
    if (result.kind === 'session') return;
    const model = result.model;
    model.rawHealth = result.body && result.body.data ? result.body.data : null;
    state.data.overview[spec.key] = model;
    paintCard(view, model, {
      retryLabel: model.state === 'READY' ? '刷新' : '重试',
      items: overviewPreview(spec.key, model),
      cached: result.cached === true
    });
    updateOverviewSummary();
    announce(spec.title + '：' + model.label + '。');
  }

  function updateOverviewSummary() {
    const node = dom.view.querySelector('[data-overview-summary]');
    if (!node) return;
    const models = Object.keys(state.data.overview).map(function (key) {
      return state.data.overview[key];
    }).filter(Boolean);
    if (!models.length) { node.textContent = '正在读取总览…'; return; }
    const summary = M.attentionSummary(models);
    node.textContent = summary.headline;
  }

  // ---------------------------------------------------------------- courses

  function renderCoursesPage() {
    const form = h('form', {class: 'toolbar', dataset: {form: 'courses'},
                            novalidate: true});
    const grid = h('div', {class: 'filter-grid'},
      semesterField(state.filters.courses.semester, '全部学期'),
      h('div', {class: 'field'},
        h('label', {for: 'course-kind'}, '类型'),
        h('select', {id: 'course-kind', dataset: {filter: 'kind'}},
          [['', '全部'], ['student', '学生'], ['teacher', '教师'], ['other', '其他']]
            .map(function (pair) {
              return h('option', {value: pair[0], selected: pair[0] === state.filters.courses.kind},
                pair[1]);
            }))),
      h('div', {class: 'field'},
        h('label', {for: 'course-query'}, '关键词'),
        h('input', {id: 'course-query', type: 'search', dataset: {filter: 'query'},
          value: state.filters.courses.query, placeholder: '课程名或课程编号',
          autocomplete: 'off'})));
    append(form, [grid, h('div', {class: 'button-row'},
      h('button', {type: 'submit', class: 'primary', dataset: {action: 'apply-filters'}},
        '应用筛选'),
      actionButton('重置', 'reset-courses', 'courses', 'ghost'),
      actionButton('刷新', 'refresh-courses', 'courses', 'ghost'))]);
    const results = h('div', {class: 'section', dataset: {courseResults: 'true'}});
    replace(dom.view, [form, results]);
    const ensure = state.semesters.loaded
      ? Promise.resolve(state.semesters.options) : loadSemesters();
    ensure.then(function () {
      if (state.route !== 'courses') return;
      renderCoursesToolbar();
      loadCourses();
    });
  }

  function renderCoursesToolbar() {
    const form = dom.view.querySelector('form[data-form="courses"]');
    if (!form) return;
    const old = form.querySelector('[data-filter="semester"]');
    if (!old) return;
    const field = old.closest('.field');
    const replacement = semesterField(state.filters.courses.semester, '全部学期');
    if (field) field.replaceWith(replacement);
  }

  async function loadCourses(force) {
    const results = dom.view.querySelector('[data-course-results]');
    if (!results) return;
    replace(results, [h('h2', {}, '课程列表'), loadingBlock('正在读取课程…',
      function () { abortKey('courses'); }, 'courses')]);
    const params = {};
    if (state.filters.courses.semester) params.semester = state.filters.courses.semester;
    if (state.filters.courses.query) params.query = state.filters.courses.query;
    const result = await request('courses', M.API.query + 'learn/courses',
      {parameters: JSON.stringify(params)}, {label: '课程', cache: force !== true});
    if (result.kind === 'session' || state.route !== 'courses') return;
    if (result.kind === 'cancelled') {
      replace(results, [h('h2', {}, '课程列表'), emptyBlock('本次读取已取消。')]);
      return;
    }
    const model = result.model;
    const filtered = M.filterCourses(model.items, state.filters.courses);
    const summary = h('p', {class: 'section-note'},
      model.label + '；显示 ' + filtered.length + ' 门课程' +
      (model.paging.total !== null ? '（来源共 ' + model.paging.total + ' 门）' : '') +
      (result.cached ? '；10 秒内复用上次读取结果' : '') + '。');
    const body = h('div', {}, stateBlock(model));
    if (!filtered.length) {
      append(body, emptyBlock(model.state === 'EMPTY'
        ? '没有符合条件的课程。' : '当前筛选下没有课程。'));
    } else {
      const list = h('ul', {class: 'item-list'});
      filtered.forEach(function (row) {
        const courseId = M.text(row.course_id, 128);
        const teachers = Array.isArray(row.teacher_names) ? row.teacher_names.join('、')
          : M.text(row.teacher_name || row.teacherName, 160);
        const item = h('li', {class: 'item', dataset: {course: courseId || 'synthetic'}},
          h('p', {class: 'item-title', text: M.text(row.course_name, 160) || '未命名课程'}),
          h('p', {class: 'item-meta',
            text: [courseId, M.text(row.semester, 40), M.KIND_LABELS[M.courseKind(row)],
                   teachers].filter(Boolean).join(' · ')}));
        if (courseId) {
          append(item, h('div', {class: 'button-row'},
            h('button', {type: 'button', class: 'ghost', dataset:
              {action: 'open-resources', courseId: courseId,
               semester: M.text(row.semester, 40)}}, '在资料中查看')));
        }
        list.append(item);
      });
      append(body, list);
    }
    replace(results, [h('h2', {}, '课程列表'), summary, body]);
    state.data.courses = model;
    announce('课程：' + model.label + '，显示 ' + filtered.length + ' 门。');
  }

  // ---------------------------------------------------------------- resources

  function renderResourcesPage() {
    const form = h('form', {class: 'toolbar', dataset: {form: 'resources'},
                            novalidate: true});
    const grid = h('div', {class: 'filter-grid'},
      semesterField(state.filters.resources.semester, '请选择学期'),
      h('div', {class: 'field'},
        h('label', {for: 'resource-course'}, '课程编号'),
        h('input', {id: 'resource-course', type: 'text', dataset: {filter: 'courseId'},
          value: state.filters.resources.courseId, placeholder: '可留空',
          autocomplete: 'off'})),
      h('div', {class: 'field'},
        h('label', {for: 'resource-kind'}, '类型'),
        h('select', {id: 'resource-kind', dataset: {filter: 'kind'}},
          [['', '全部'], ['PDF', 'PDF'], ['文档', '文档'], ['表格', '表格'], ['演示', '演示'],
           ['图片', '图片'], ['压缩包', '压缩包'], ['文本', '文本'], ['其他', '其他']]
            .map(function (pair) {
              return h('option', {value: pair[0], selected: pair[0] === state.filters.resources.kind},
                pair[1]);
            }))),
      h('div', {class: 'field'},
        h('label', {for: 'resource-query'}, '关键词'),
        h('input', {id: 'resource-query', type: 'search', dataset: {filter: 'query'},
          value: state.filters.resources.query, placeholder: '资料名、课程或编号',
          autocomplete: 'off'})));
    append(form, [grid, h('div', {class: 'button-row'},
      h('button', {type: 'submit', class: 'primary', dataset: {action: 'apply-filters'}},
        '应用筛选'),
      actionButton('清除选择', 'clear-selection', '', 'ghost'),
      actionButton('刷新', 'refresh-resources', '', 'ghost'))]);
    const results = h('div', {class: 'section', dataset: {resourceResults: 'true'}});
    const selection = h('div', {class: 'selection-panel', dataset: {selectionPanel: 'true'}});
    replace(dom.view, [form, selection, results]);
    const ensure = state.semesters.loaded
      ? Promise.resolve(state.semesters.options) : loadSemesters();
    ensure.then(function () {
      if (state.route !== 'resources') return;
      renderResourcesToolbar();
      renderResourceSelection();
      if (state.filters.resources.semester) loadResources(true);
      else renderResourcesPrompt();
    });
  }

  function renderResourcesToolbar() {
    const form = dom.view.querySelector('form[data-form="resources"]');
    if (!form) return;
    const old = form.querySelector('[data-filter="semester"]');
    if (!old) return;
    const field = old.closest('.field');
    const replacement = semesterField(state.filters.resources.semester, '请选择学期');
    if (field) field.replaceWith(replacement);
  }

  function renderResourcesPrompt() {
    const results = dom.view.querySelector('[data-resource-results]');
    if (!results) return;
    replace(results, [h('h2', {}, '资料列表'),
      emptyBlock('需要先选择学期。资料索引按学期读取，不会用默认学期冒充结果。')]);
  }

  function resourceRow(row) {
    const uid = M.stableUid(row);
    const selected = uid && state.resources.selection.has(uid);
    const box = h('input', {type: 'checkbox', dataset: {selectUid: uid || ''},
                            'aria-label': '选择资料 ' + (M.text(row.title, 120) || '未命名资料')});
    box.checked = Boolean(selected);
    box.disabled = !uid;
    const linkValue = row.source_link || row.official_url || row.entry_url;
    const meta = [M.resourceType(row), M.text(row.course_name, 120), M.text(row.semester, 40),
                  M.formatBytes(row.size_bytes), M.statusLabel(row.state)].filter(Boolean).join(' · ');
    const item = h('li', {class: 'resource' + (uid ? '' : ' disabled'),
                          dataset: {resource: uid || 'no-uid', uid: uid}},
      box,
      h('div', {class: 'resource-body'},
        h('p', {class: 'resource-title', text: M.text(row.title, 240) || '未命名资料'}),
        h('p', {class: 'resource-meta', text: meta}),
        linkValue ? h('p', {class: 'resource-link'}, safeLink('打开资料来源', linkValue)) : ''));
    if (uid) {
      box.addEventListener('change', function () {
        if (box.checked) {
          state.resources.selection.add(uid);
          state.resources.titles.set(uid, M.text(row.title, 160) || uid);
          announce('已选择 ' + state.resources.selection.size + ' 项资料（跨页保留）。');
        } else {
          state.resources.selection.remove(uid);
          announce('已取消选择，剩余 ' + state.resources.selection.size + ' 项。');
        }
        renderResourceSelection();
      });
    }
    return item;
  }

  function renderResourceSelection() {
    const panel = dom.view.querySelector('[data-selection-panel]');
    if (!panel) return;
    const plan = M.selectionPlan(state.resources.selection, state.filters.resources,
      state.resources.loaded, state.resources.total);
    const selected = state.resources.selection.values();
    const titles = selected.slice(0, 5).map(function (uid) {
      return state.resources.titles.get(uid) || uid;
    });
    const parts = [
      h('p', {}, h('span', {class: 'selection-count', dataset: {selectionCount: 'true'}},
        String(plan.count)), ' 项已选择（跨页保留，按稳定编号识别）。'),
      selected.length
        ? h('ul', {class: 'selection-list'}, titles.map(function (title) {
          return h('li', {text: '· ' + title});
        }))
        : h('p', {class: 'selection-list', text: '尚未选择资料。'}),
      h('p', {class: 'selection-list', dataset: {plan: 'true'},
        text: '导出范围：当前筛选条件下已加载 ' + plan.loaded + ' 条' +
          (plan.total !== null ? '，来源共 ' + plan.total + ' 条' : '') + '。' + plan.note})
    ];
    replace(panel, parts);
  }

  async function loadResources(reset, force) {
    if (state.resources.loading) return;
    if (!state.filters.resources.semester) { renderResourcesPrompt(); return; }
    const results = dom.view.querySelector('[data-resource-results]');
    if (!results) return;
    state.resources.loading = true;
    if (reset) {
      state.resources.items = [];
      state.resources.cursor = null;
      state.resources.cursorFilters = null;
      state.resources.total = null;
      if (!force) {
        state.resources.selection.reset();
        state.resources.titles.clear();
      }
      state.resources.loaded = 0;
      state.resources.snapshot += 1;
      replace(results, [h('h2', {}, '资料列表'), loadingBlock('正在读取资料…',
        function () { abortKey('resources'); }, 'resources')]);
      renderResourceSelection();
    } else {
      const more = results.querySelector('[data-action="load-more"]');
      if (more) { more.disabled = true; more.textContent = '正在加载…'; }
    }
    const cursor = reset ? null : state.resources.cursor;
    const params = M.resourceQuery(state.filters.resources, cursor);
    const result = await request('resources', M.API.resources, params,
      {label: '课程资料', cache: force !== true});
    state.resources.loading = false;
    if (result.kind === 'session' || state.route !== 'resources') return;
    if (result.kind === 'cancelled') {
      replace(results, [h('h2', {}, '资料列表'), emptyBlock('本次读取已取消。')]);
      renderResourceSelection();
      return;
    }
    const model = result.model;
    if (model.state !== 'READY' && model.state !== 'EMPTY' && model.state !== 'PARTIAL'
        && state.resources.items.length === 0) {
      replace(results, [h('h2', {}, '资料列表'), stateBlock(model),
        h('div', {class: 'button-row'},
          actionButton('重试', 'refresh-resources', '', 'ghost'))]);
      state.data.resources = model;
      renderResourceSelection();
      announce('课程资料：' + model.label + '。');
      return;
    }
    const existing = new Set(state.resources.items.map(function (row) {
      const uid = M.stableUid(row);
      return uid ? 'uid:' + uid : '';
    }).filter(Boolean));
    let added = 0;
    model.items.forEach(function (row) {
      const uid = M.stableUid(row);
      const key = uid ? 'uid:' + uid : '';
      if (key && existing.has(key)) return;
      if (key) existing.add(key);
      state.resources.items.push(row);
      added += 1;
    });
    state.resources.cursor = result.body && result.body.next_cursor ? result.body.next_cursor : null;
    state.resources.cursorFilters = Object.assign({}, state.filters.resources);
    state.resources.total = model.paging.total;
    state.resources.loaded = state.resources.items.length;
    state.data.resources = model;
    const filtered = M.filterResources(state.resources.items, state.filters.resources);
    const header = h('div', {},
      h('h2', {}, '资料列表'),
      h('p', {class: 'section-note'},
        model.label + '；已加载 ' + state.resources.items.length + ' 条' +
        (state.resources.total !== null ? '，来源共 ' + state.resources.total + ' 条' : '') +
        '；本次新增 ' + added + ' 条' +
        (result.cached ? '；10 秒内复用上次读取结果' : '') + '。'),
      stateBlock(model));
    if (!filtered.length) {
      append(header, emptyBlock(model.state === 'EMPTY' ? '没有符合条件的资料。'
        : '当前筛选下没有资料。'));
    } else {
      append(header, h('ul', {class: 'resource-list'},
        filtered.map(function (row) { return resourceRow(row); })));
    }
    if (state.resources.cursor) {
      append(header, h('div', {class: 'button-row'},
        h('button', {type: 'button', dataset: {action: 'load-more'}}, '加载更多')));
    }
    replace(results, header);
    renderResourceSelection();
    announce('课程资料：' + model.label + '，已加载 ' + state.resources.items.length + ' 条。');
  }

  function applyResourceFilters() {
    const form = dom.view.querySelector('form[data-form="resources"]');
    if (!form) return;
    const values = readFilters(form);
    const next = M.nextQuery(state.filters.resources, {
      semester: values.semester || '',
      courseId: values.courseId || '',
      kind: values.kind || '',
      query: values.query || ''
    });
    const filtersChanged = next.changed;
    state.filters.resources = next.filters;
    if (filtersChanged) {
      state.resources.selection.reset();
      state.resources.titles.clear();
      state.resources.cursor = null;
      state.resources.cursorFilters = null;
      state.resources.items = [];
      state.resources.total = null;
    } else if (state.resources.items.length) {
      announce('筛选条件未变化；已选择的资料和当前分页保持不变。');
      return;
    }
    renderResourceSelection();
    if (!state.filters.resources.semester) { renderResourcesPrompt(); return; }
    loadResources(true);
  }

  // ---------------------------------------------------------------- calendar

  function renderCalendarPage() {
    const window = localTimeWindow();
    if (!state.filters.calendar.startDate) state.filters.calendar.startDate = window.start;
    if (!state.filters.calendar.endDate) state.filters.calendar.endDate = window.end;
    const form = h('form', {class: 'toolbar', dataset: {form: 'calendar'}, novalidate: true});
    const toggles = h('div', {class: 'toggle-group', role: 'group', 'aria-label': '日程视图'},
      h('button', {type: 'button', dataset: {action: 'calendar-mode', mode: 'timeline'},
        'aria-pressed': state.filters.calendar.mode === 'timeline' ? 'true' : 'false'}, '时间线'),
      h('button', {type: 'button', dataset: {action: 'calendar-mode', mode: 'list'},
        'aria-pressed': state.filters.calendar.mode === 'list' ? 'true' : 'false'}, '列表'));
    append(form, [
      h('div', {class: 'filter-grid'},
        h('div', {class: 'field'},
          h('label', {for: 'calendar-start'}, '开始日期'),
          h('input', {id: 'calendar-start', type: 'date', dataset: {filter: 'startDate'},
            value: state.filters.calendar.startDate})),
        h('div', {class: 'field'},
          h('label', {for: 'calendar-end'}, '结束日期'),
          h('input', {id: 'calendar-end', type: 'date', dataset: {filter: 'endDate'},
            value: state.filters.calendar.endDate})),
        h('div', {class: 'field'}, h('span', {class: 'field-label'}, '视图'), toggles)),
      h('div', {class: 'button-row'},
        h('button', {type: 'submit', class: 'primary', dataset: {action: 'apply-filters'}},
          '应用范围'),
        actionButton('刷新', 'refresh-calendar', '', 'ghost'))
    ]);
    const results = h('div', {class: 'section', dataset: {calendarResults: 'true'}});
    replace(dom.view, [form, results]);
    loadCalendar();
  }

  async function loadCalendar(force) {
    const results = dom.view.querySelector('[data-calendar-results]');
    if (!results) return;
    replace(results, [h('h2', {}, '日程视图'), loadingBlock('正在读取日程…',
      function () { abortKey('calendar'); }, 'calendar')]);
    const result = await request('calendar', M.API.calendar, {
      start_date: state.filters.calendar.startDate,
      end_date: state.filters.calendar.endDate,
      page_size: M.PAGE_SIZE
    }, {label: '日程', cache: force !== true});
    if (result.kind === 'session' || state.route !== 'calendar') return;
    if (result.kind === 'cancelled') {
      replace(results, [h('h2', {}, '日程视图'), emptyBlock('本次读取已取消。')]);
      return;
    }
    state.data.calendar = result.body ? M.calendarModel(result.body, new Date().toISOString())
      : null;
    state.data.calendarEnvelope = result.model;
    state.calendarCached = result.cached === true;
    renderCalendarBody();
    announce('日程：' + result.model.label + '。');
  }

  function renderCalendarBody() {
    const results = dom.view.querySelector('[data-calendar-results]');
    if (!results) return;
    const model = state.data.calendar;
    const envelope = state.data.calendarEnvelope;
    if (!model && envelope) {
      replace(results, [h('h2', {}, '日程视图'), stateBlock(envelope)]);
      return;
    }
    if (!model) return;
    const mode = state.filters.calendar.mode;
    const container = h('div', {dataset: {calendarMode: mode}});
    append(container, h('p', {class: 'section-note'},
      envelope.label + '；窗口 ' + state.filters.calendar.startDate + ' 至 ' +
      state.filters.calendar.endDate + '；逾期 ' + model.counts.overdue + '，今天 ' +
      model.counts.today + '，即将 ' + model.counts.upcoming + '，无日期 ' +
      model.counts.undated + '，已完成 ' + model.counts.completed + '。' +
      (state.calendarCached ? ' 10 秒内复用上次读取结果。' : '')));
    append(container, stateBlock(envelope));
    if (model.conflicts.length) {
      append(container, h('div', {class: 'conflict-box', dataset: {conflictCount:
        String(model.conflicts.length)}},
        h('p', {text: '检测到 ' + model.conflicts.length + ' 处可信时间段冲突（仅时间段参与）。'}),
        h('ul', {}, model.conflicts.map(function (conflict) {
          return h('li', {text: conflict.titles.join(' × ') + '：' + conflict.startLabel +
            ' 至 ' + conflict.endLabel});
        }))));
    }
    if (mode === 'timeline') append(container, renderTimeline(model));
    else append(container, renderCalendarList(model));
    if (model.limitations.length) {
      append(container, h('div', {class: 'limitations', dataset: {limitation: 'true'}},
        h('p', {}, '限制与说明'),
        h('ul', {}, model.limitations.map(function (line) { return h('li', {text: line}); }))));
    }
    replace(results, [h('h2', {}, '日程视图'), container]);
    syncCalendarToggles();
  }

  function syncCalendarToggles() {
    Array.from(dom.view.querySelectorAll('[data-action="calendar-mode"]')).forEach(function (button) {
      button.setAttribute('aria-pressed', button.dataset.mode === state.filters.calendar.mode
        ? 'true' : 'false');
    });
  }

  const BUCKETS = [
    ['overdue', '逾期'], ['today', '今天'], ['upcoming', '即将'],
    ['undated', '无日期'], ['completed', '已完成']
  ];

  function eventNode(item) {
    const meta = [item.status, item.kind === 'DATE' ? '日期级' : '精确时间',
                  item.source, item.location].filter(Boolean).join(' · ');
    return h('li', {class: 'event', dataset: {event: item.uid || 'synthetic',
      kind: item.kind, overdue: item.overdue ? 'true' : 'false'}},
      h('p', {class: 'event-time', text: item.dateLabel + ' · ' + item.timeLabel}),
      h('div', {class: 'event-body'},
        h('p', {class: 'event-title', text: item.title}),
        h('p', {class: 'event-meta', text: meta}),
        item.limitation
          ? h('p', {class: 'item-note', dataset: {limitationItem: 'true'},
            text: '缺少校历或节次表：仅保留来源周期，不推断具体时间。'})
          : '',
        item.sourceUrl ? h('p', {class: 'resource-link'},
          safeLink('打开来源', item.sourceUrl)) : ''));
  }

  const SEMANTICS_LABELS = {DATE: '日期', DATE_TIME: '时间段', DEADLINE: '截止',
                            INTERVAL: '时间段', OCCURRENCE: '周期', UNKNOWN: '未知'};

  function unknownEventNode(item) {
    const cycle = SEMANTICS_LABELS[item.semantics] || item.semantics || '未知';
    return h('li', {class: 'event', dataset: {event: item.uid || 'synthetic', kind: 'UNKNOWN'}},
      h('p', {class: 'event-time', text: '时间未知'}),
      h('div', {class: 'event-body'},
        h('p', {class: 'event-title', text: item.title}),
        h('p', {class: 'event-meta', text: item.status}),
        h('p', {class: 'item-meta',
          text: '原始周期：' + cycle + (item.limitation ? '（缺少校历或节次表，未展开）' : '')}),
        h('p', {class: 'item-note', text: '来源没有可用日期；不编造日期。'})));
  }

  function renderTimeline(model) {
    const wrapper = h('div', {class: 'timeline'});
    let rendered = 0;
    BUCKETS.forEach(function (pair) {
      const items = model.buckets[pair[0]];
      if (!items.length) return;
      rendered += items.length;
      const group = h('section', {class: 'timeline-day', dataset: {bucket: pair[0]}},
        h('h3', {text: pair[1] + '（' + items.length + '）'}));
      const byDate = new Map();
      items.forEach(function (item) {
        const key = pair[0] === 'undated' ? 'undated' : (item.dateKey || 'unknown');
        if (!byDate.has(key)) byDate.set(key, []);
        byDate.get(key).push(item);
      });
      byDate.forEach(function (list, dateKey) {
        if (dateKey !== 'undated' && dateKey !== 'unknown') {
          group.append(h('h4', {class: 'timeline-date', text: M.formatDate(dateKey)}));
        } else {
          group.append(h('h4', {class: 'timeline-date', text: '时间未知'}));
        }
        const ul = h('ul', {class: 'timeline-events'});
        list.forEach(function (item) {
          if (pair[0] === 'undated' || !item.valid) ul.append(unknownEventNode(item));
          else ul.append(eventNode(item));
        });
        group.append(ul);
      });
      wrapper.append(group);
    });
    if (!rendered) wrapper.append(emptyBlock('所选时间范围内没有日程。'));
    return wrapper;
  }

  function renderCalendarList(model) {
    const wrapper = h('div', {class: 'timeline'});
    const all = [];
    BUCKETS.forEach(function (pair) {
      model.buckets[pair[0]].forEach(function (item) { all.push(item); });
    });
    if (!all.length) return emptyBlock('所选时间范围内没有日程。');
    const list = h('ul', {class: 'timeline-events'});
    all.forEach(function (item) {
      const node = eventNode(item);
      node.dataset.date = item.dateKey || 'unknown';
      list.append(node);
    });
    wrapper.append(list);
    return wrapper;
  }

  // ---------------------------------------------------------------- library

  function renderLibraryPage() {
    const intro = h('p', {class: 'section-note'},
      '只读取既有记录；本页没有预约、取消、续借或付款按钮。办理请使用原有 MCP/CLI 受控流程。');
    const container = h('div', {class: 'cards', dataset: {libraryCards: 'true'}});
    replace(dom.view, [intro, container]);
    const sections = [
      {key: 'loans', title: '借阅', path: M.API.query + 'library/loans'},
      {key: 'reservations', title: '图书馆预约', path: M.API.query + 'library/reservations'},
      {key: 'orders', title: '体育预约', path: M.API.query + 'sports/orders'}
    ];
    sections.forEach(function (section) {
      const view = cardShell(section.key, section.title, 'library');
      container.append(view.article);
      loadLibrarySection(section, view);
    });
    const entries = h('section', {class: 'section', dataset: {officialEntries: 'true'}},
      h('h2', {}, '官方入口'),
      h('p', {class: 'section-note', text: '以下为第一方 HTTPS 入口；看板只打开链接，不携带访问令牌。'}),
      h('div', {class: 'link-list'}, M.OFFICIAL_ENTRIES.map(function (entry) {
        return safeLink(entry.label, entry.url);
      })));
    append(dom.view, entries);
  }

  function libraryItem(section, row) {
    const title = M.text(row.title || row.book_title || row.name || row.room_name ||
      row.scene_name || row.venue, 200) || '未命名记录';
    const when = row.due_at || row.due_date || row.start || row.start_at || row.date;
    const parts = [(section === 'loans' ? '应还：' : '时间：') +
      (when ? M.formatTimestamp(when) : '未知'), M.statusLabel(row.status),
      M.text(row.location, 120)].filter(Boolean);
    const link = row.official_url || row.entry_url || row.source_link;
    return h('li', {class: 'item'},
      h('p', {class: 'item-title', text: title}),
      h('p', {class: 'item-meta', text: parts.join(' · ')}),
      link ? h('p', {class: 'resource-link'}, safeLink('官方入口', link)) : '');
  }

  async function loadLibrarySection(section, view, force) {
    const key = 'library-' + section.key;
    setCardLoading(view, key);
    const result = await request(key, section.path, {parameters: '{}'},
      {label: section.title, cache: force !== true});
    if (result.kind === 'session' || state.route !== 'library') return;
    if (result.kind === 'cancelled') {
      view.article.dataset.cardState = 'CANCELLED';
      replace(view.body, emptyBlock('本次读取已取消。'));
      replace(view.actions, [actionButton('重试', 'refresh-card', section.key)]);
      return;
    }
    const model = result.model;
    state.data.library[section.key] = model;
    const items = model.items.length
      ? h('ul', {class: 'item-list'}, model.items.map(function (row) {
        return libraryItem(section.key, row);
      }))
      : emptyBlock(model.state === 'EMPTY' ? '没有记录。' : '暂无可显示记录。');
    paintCard(view, model, {retryLabel: model.state === 'READY' ? '刷新' : '重试',
      items: items, cached: result.cached === true});
    announce(section.title + '：' + model.label + '。');
  }

  // ---------------------------------------------------------------- health

  function renderHealthPage() {
    const container = h('div', {dataset: {healthResults: 'true'}});
    const services = h('section', {class: 'section', dataset: {serviceCatalog: 'true'}});
    replace(dom.view, [container, services]);
    loadHealth(container, services);
  }

  async function loadHealth(container, services, force) {
    replace(container, [h('h2', {}, '服务健康'), loadingBlock('正在读取健康报告…',
      function () { abortKey('health-page'); }, 'health-page')]);
    const result = await request('health-page', M.API.health, {},
      {label: '来源健康', cache: force !== true});
    if (result.kind === 'session' || state.route !== 'health') return;
    if (result.kind === 'cancelled') {
      replace(container, [h('h2', {}, '服务健康'), emptyBlock('本次读取已取消。')]);
      return;
    }
    const model = M.healthModel(result.body || {});
    state.data.health = model;
    const header = h('div', {},
      h('h2', {}, '服务健康'),
      h('p', {class: 'section-note', dataset: {healthCount: 'true'},
        text: '服务 ' + model.counts.total + ' 个；已实现 ' + model.counts.implemented +
          ' 个，阻塞 ' + model.counts.blocked + ' 个，未实现 ' + model.counts.missing + ' 个。' +
          (result.cached ? ' 10 秒内复用上次读取结果。' : '')}),
      stateBlock(model.model));
    if (!model.rows.length) {
      append(header, emptyBlock('健康报告没有返回服务条目。'));
    } else {
      const table = h('div', {class: 'health-table'});
      model.rows.forEach(function (row) {
        table.append(h('div', {class: 'health-row', dataset: {healthRow: row.serviceId}},
          h('div', {class: 'health-cell'},
            h('span', {class: 'label'}, '服务'),
            h('span', {class: 'value', text: row.serviceId || '未知'})),
          h('div', {class: 'health-cell'},
            h('span', {class: 'label'}, '状态'),
            h('span', {class: 'value', text: row.status})),
          h('div', {class: 'health-cell'},
            h('span', {class: 'label'}, '实现'),
            h('span', {class: 'value'}, h('span', {class: 'badge badge-' +
              row.implementationTone, dataset: {implementation: row.implementation}},
              row.implementationLabel))),
          h('div', {class: 'health-cell'},
            h('span', {class: 'label'}, '运行'),
            h('span', {class: 'value', text: row.runtimeLabel})),
          h('div', {class: 'health-cell'},
            h('span', {class: 'label'}, '证据'),
            h('span', {class: 'value', text: row.evidenceLabel})),
          h('div', {class: 'health-cell'},
            h('span', {class: 'label'}, '检查时间'),
            h('span', {class: 'value', text: row.checkedLabel}))));
      });
      append(header, table);
    }
    append(header, h('div', {class: 'limitations'},
      h('p', {}, '本地读取不等于校园可用；健康报告不探测真实站点，也不代表真实业务写入权限。')));
    replace(container, header);
    announce('来源健康：' + model.model.label + '。');
    loadServiceCatalog(services, force);
  }

  async function loadServiceCatalog(services, force) {
    if (!services) return;
    replace(services, [h('h2', {}, '服务目录'), loadingBlock('正在读取服务目录…',
      function () { abortKey('services-page'); }, 'services-page')]);
    const result = await request('services-page', M.API.services, {},
      {label: '服务目录', cache: force !== true});
    if (result.kind === 'session' || state.route !== 'health') return;
    if (result.kind === 'cancelled') {
      replace(services, [h('h2', {}, '服务目录'), emptyBlock('本次读取已取消。')]);
      return;
    }
    const model = result.model;
    state.data.services = model;
    const body = h('div', {}, stateBlock(model));
    if (model.items.length) {
      append(body, h('ul', {class: 'item-list'}, model.items.map(function (row) {
        return h('li', {class: 'item', dataset: {service: M.text(row.id, 80)}},
          h('p', {class: 'item-title', text: M.text(row.name, 120) || '未命名服务'}),
          h('p', {class: 'item-meta', text: [M.text(row.id, 80),
            M.text(row.category, 80), M.text(row.description, 200)].filter(Boolean).join(' · ')}));
      })));
    } else {
      append(body, emptyBlock('服务目录没有条目。'));
    }
    replace(services, [h('h2', {}, '服务目录'), body]);
    announce('服务目录：' + model.label + '。');
  }

  // ---------------------------------------------------------------- session

  function sessionLost(reason) {
    abortAll(true);
    state.session = 'lost';
    state.cache.clear();
    state.data = {overview: {}, courses: null, resources: null, calendar: null,
                  calendarEnvelope: null, library: {}, health: null, services: null};
    state.semesters = {loaded: false, loading: null, options: [], manual: false, error: null};
    state.resources.items = [];
    state.resources.cursor = null;
    state.resources.cursorFilters = null;
    state.resources.total = null;
    state.resources.loaded = 0;
    state.resources.selection.reset();
    state.resources.titles.clear();
    dom.banner.hidden = false;
    const expired = reason !== 'unreachable';
    replace(dom.banner, [
      h('p', {}, expired ? '本地看板会话已失效，已清除本页显示的旧数据。'
        : '无法连接本地看板服务，已清除本页显示的旧数据。'),
      h('p', {}, expired
        ? '重新打开本地看板以建立新会话；不需要重新登录学校，也没有任何密码输入框。'
        : '确认看板进程仍在运行，再重新打开本页。'),
      h('div', {class: 'button-row'},
        h('button', {type: 'button', class: 'primary', dataset: {action: 'reopen'}},
          '重新打开看板'))
    ]);
    renderSessionLostView();
    announce(expired ? '本地会话已失效，旧数据已清除。' : '本地服务不可达，旧数据已清除。');
  }

  function renderSessionLostView() {
    replace(dom.view, h('div', {class: 'limitations', dataset: {sessionLost: 'true'}},
      h('p', {}, '页面已停止显示任何来源数据。'),
      h('p', {}, '本地会话由一次性引导建立；重新打开看板会建立新会话。')));
  }

  // ---------------------------------------------------------------- events

  function handleAction(event) {
    const target = event.target.closest('[data-action]');
    if (!target) return;
    const action = target.dataset.action;
    if (action === 'reopen') { location.reload(); return; }
    if (action === 'cancel') { abortKey(target.dataset.key); return; }
    if (action === 'cancel-all') { abortAll(); return; }
    if (action === 'refresh-card') { refreshCard(target.dataset.key); return; }
    if (action === 'refresh-courses') { loadCourses(true); return; }
    if (action === 'refresh-resources') { loadResources(true, true); return; }
    if (action === 'refresh-calendar') { loadCalendar(true); return; }
    if (action === 'load-more') { loadMoreResources(); return; }
    if (action === 'clear-selection') {
      state.resources.selection.clear();
      state.resources.titles.clear();
      renderResourceSelection();
      announce('已清除全部选择；分页和筛选状态保持不变。');
      return;
    }
    if (action === 'calendar-mode') {
      state.filters.calendar.mode = target.dataset.mode === 'list' ? 'list' : 'timeline';
      renderCalendarBody();
      return;
    }
    if (action === 'open-resources') {
      state.filters.resources.courseId = target.dataset.courseId || '';
      if (target.dataset.semester) {
        state.filters.resources.semester = target.dataset.semester;
      }
      location.hash = '#/resources';
      return;
    }
    if (action === 'reset-courses') {
      state.filters.courses = {semester: '', query: '', kind: ''};
      renderCoursesPage();
    }
  }

  function refreshCard(key) {
    const spec = OVERVIEW_CARDS.filter(function (row) { return row.key === key; })[0];
    if (spec && state.route === 'overview') {
      const article = dom.view.querySelector('[data-card="' + key + '"]');
      if (article) {
        const view = {key: key, title: spec.title, page: spec.page, article: article,
          head: article.querySelector('.card-head'), meta: article.querySelector('.meta'),
          body: article.children[2], extra: article.children[3],
          actions: article.querySelector('.button-row')};
        loadOverviewCard(spec, view, true);
      }
      return;
    }
    if (state.route === 'library') {
      const section = {
        loans: {key: 'loans', title: '借阅', path: M.API.query + 'library/loans'},
        reservations: {key: 'reservations', title: '图书馆预约',
                       path: M.API.query + 'library/reservations'},
        orders: {key: 'orders', title: '体育预约', path: M.API.query + 'sports/orders'}
      }[key];
      const article = dom.view.querySelector('[data-card="' + key + '"]');
      if (section && article) {
        const view = {key: key, title: section.title, page: 'library', article: article,
          head: article.querySelector('.card-head'), meta: article.querySelector('.meta'),
          body: article.children[2], extra: article.children[3],
          actions: article.querySelector('.button-row')};
        loadLibrarySection(section, view, true);
      }
    }
  }

  function loadMoreResources() {
    if (!state.resources.cursor) return;
    const current = JSON.stringify(state.filters.resources);
    const cursorFilters = state.resources.cursorFilters
      ? JSON.stringify(state.resources.cursorFilters) : '';
    if (current !== cursorFilters) {
      // The cursor belongs to the previous snapshot; never mix pages.
      state.resources.cursor = null;
      loadResources(true);
      return;
    }
    loadResources(false);
  }

  function handleSubmit(event) {
    const form = event.target.closest('form[data-form]');
    if (!form) return;
    event.preventDefault();
    if (form.dataset.form === 'courses') {
      const values = readFilters(form);
      const next = M.nextQuery(state.filters.courses, {
        semester: values.semester || '', kind: values.kind || '', query: values.query || ''
      });
      state.filters.courses = next.filters;
      loadCourses();
    } else if (form.dataset.form === 'resources') {
      applyResourceFilters();
    } else if (form.dataset.form === 'calendar') {
      const values = readFilters(form);
      const changed = values.startDate !== state.filters.calendar.startDate
        || values.endDate !== state.filters.calendar.endDate;
      state.filters.calendar.startDate = values.startDate;
      state.filters.calendar.endDate = values.endDate;
      if (changed) loadCalendar();
      else renderCalendarBody();
    }
  }

  // ---------------------------------------------------------------- boot

  function boot() {
    renderNav();
    document.addEventListener('click', handleAction);
    document.addEventListener('submit', handleSubmit);
    window.addEventListener('hashchange', route);
    window.addEventListener('pagehide', function () {
      state.cache.clear();
      state.data = {overview: {}, courses: null, resources: null, calendar: null,
                    library: {}, health: null, services: null};
      state.resources.items = [];
      state.resources.selection.clear();
      state.resources.titles.clear();
    });
    loadSemesters();
    route();
  }

  boot();
}());
