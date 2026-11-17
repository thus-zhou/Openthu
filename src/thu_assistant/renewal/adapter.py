"""Seat/study-room independent loan renewal: eligibility, binding and readback.

Only the read side and one explicitly contracted request live here.  With no
verified source contract the plan carries ``RENEWAL_SOURCE_CONTRACT_UNVERIFIED``
and is handed to the official MyLib page; nothing is POSTed and no local state
claims that a loan was renewed.  Success requires the same loan record to come
back from a complete own-loan read with a *new* later due time.
"""
import re
import time
from urllib.parse import urlsplit

from .. import library as lib, library_contracts as c, precheck
from ..actions import NotDispatched, digest
from ..bridge import CampusError
from . import contract as renewal_contract, transport

LOANS_HOST = 'discover.lib.tsinghua.edu.cn'
LOANS_PATH = '/entrance/myLibEntrance/myLibIndex/MyLibBorrowNow'
OFFICIAL = 'https://' + LOANS_HOST + LOANS_PATH
DUE = re.compile(r'\d{4}-\d{2}-\d{2}(?:T(?:[01]\d|2[0-3]):[0-5]\d(?:\+08:00)?)?')
HINTS = 5


def require(acknowledged, code='RENEWAL_RESPONSE_INVALID'):
    if not acknowledged:
        raise CampusError(code)


class Reader:
    """One short-lived authenticated Primo context, bound to the own-loans page."""

    def __init__(self, browsers):
        self.browsers, self.sid = browsers, None

    def __enter__(self):
        try:
            opened = self.browsers.open(service_id='library-catalog', visible=False)
            self.sid = opened['session_id']
            require(opened.get('status') != 'TWO_FACTOR_REQUIRED', 'TWO_FACTOR_REQUIRED')
            self.page = self.browsers.sessions[self.sid]['page']
            parsed = urlsplit(self.page.url)
            require(parsed.scheme == 'https' and parsed.hostname == LOANS_HOST
                    and parsed.path.lower() == LOANS_PATH.lower(), 'AUTH_REQUIRED')
            self.base = parsed.scheme + '://' + parsed.netloc
            self.account = self.page.evaluate(
                "()=>{const u=JSON.parse(localStorage.getItem('userInfo')||'null');"
                "return u&&u.accountid?String(u.accountid):null}")
            require(isinstance(self.account, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,64}', self.account),
                    'AUTH_REQUIRED')
            return self
        except Exception as error:
            self.close()
            if isinstance(error, CampusError):
                raise
            raise CampusError('RENEWAL_LOGIN_FAILED') from None

    def close(self):
        if self.sid:
            self.browsers.close(self.sid)
            self.sid = None

    def __exit__(self, *args):
        self.close()


def _page_agrees(page, rows):
    """The official table and the JSON rows must describe the same page."""
    body = page.locator('.el-table__body-wrapper')
    observed = body.inner_text() if body.count() == 1 else ''
    marker = page.locator('.el-table__empty-text')
    empty = any(text.strip() in ('暂无数据', 'No Data', 'No data') for text in marker.all_text_contents())
    return all(row['title'] in observed for row in rows) if rows else empty


def read_loans(reader):
    """Read every active own loan; a partial list is never used for a write."""
    items, seen = [], set()
    for offset in range(1, 1002, 10):
        try:
            rows, more = lib.parse_loans(reader.page.evaluate(lib.LOAN_FETCH, offset))
        except CampusError:
            raise
        except Exception:
            raise CampusError('RENEWAL_LOAN_PAGE_FAILED') from None
        if offset == 1:
            require(_page_agrees(reader.page, rows), 'RENEWAL_PAGE_DISAGREES_WITH_API')
        current = {row['loan_id'] for row in rows}
        require(len(current) == len(rows) and not seen.intersection(current), 'RENEWAL_LOAN_PAGE_REPEATED')
        seen.update(current)
        items.extend(rows)
        if not more:
            return items
        require(len(rows) == 10, 'RENEWAL_LOAN_PAGINATION_UNVERIFIED')
    raise CampusError('RENEWAL_LOAN_LIST_LIMIT')


def _precheck(loan, contract):
    state = loan.get('renewal_state') or 'UNKNOWN'
    if state == 'NOT_ELIGIBLE' or loan.get('overdue') is True:
        preparation = 'NOT_PREPARABLE'
    elif contract is None:
        preparation = 'NEEDS_OFFICIAL_PAGE'
    elif state == 'ELIGIBLE':
        preparation = 'NEEDS_ELIGIBILITY_CHECK'
    else:
        preparation = 'UNKNOWN'
    return {'version': precheck.VERSION, 'operation': c.RENEW_LOAN, 'selection_state': 'UNIQUE',
            'availability_state': 'NOT_APPLICABLE',
            'eligibility_state': {'ELIGIBLE': 'ELIGIBLE', 'NOT_ELIGIBLE': 'NOT_ELIGIBLE'}.get(state, 'UNKNOWN'),
            'fees_state': 'UNKNOWN',
            'rules_state': 'READ' if loan.get('renew_flag') in ('Y', 'N') else 'MISSING',
            'preparation_state': preparation}


def loan_proposal(reader, loan_id, contract):
    """Bind exactly one own loan; read-only unless a contract was supplied."""
    loans = read_loans(reader)
    matches = [row for row in loans if row['loan_id'] == loan_id]
    require(matches, 'RENEWAL_RECORD_NOT_FOUND')
    require(len(matches) == 1, 'RENEWAL_RECORD_NOT_UNIQUE')
    loan = matches[0]
    due = loan.get('due_at')
    require(isinstance(due, str) and DUE.fullmatch(due), 'RENEWAL_DUE_DATE_UNVERIFIED')
    state = loan.get('renewal_state')
    if loan.get('overdue') is True:
        raise CampusError('RENEWAL_OVERDUE')
    if state == 'NOT_ELIGIBLE':
        raise CampusError('RENEWAL_NOT_ELIGIBLE')
    flags = []
    if state != 'ELIGIBLE':
        flags.append('RENEWAL_ELIGIBILITY_UNVERIFIED')
    if contract is None:
        flags.append('RENEWAL_SOURCE_CONTRACT_UNVERIFIED')
    hints = loan.get('renewal_hints') or []
    business = {
        'system': 'loans', 'date': due[:10], 'subject': loan.get('title') or '',
        'target': '图书馆借阅续借（当前到期 ' + due + '）',
        'record_id': loan['loan_id'], 'due_at': due,
        'renew_flag': loan.get('renew_flag') or '', 'renewal_state': state or 'UNKNOWN',
        'renewal_hint': '；'.join(str(item) for item in hints[:HINTS])[:300],
        'official_url': OFFICIAL, 'handoff': ','.join(flags),
        'rules_summary': '续借规则以官方 MyLib 页面为准；本计划只核对本人借阅记录的新到期时间。',
        'precheck': _precheck(loan, contract),
        'proof': digest([loan['loan_id'], due, loan.get('renew_flag'), hints, reader.account]),
        'source': 'library.renewal'}
    return business, loan


class RenewalAction:
    """Registered renewal adapter; execution needs an explicitly supplied contract."""

    version = c.VERSION
    accepts_ledger = False

    def __init__(self, browsers, contract=None):
        self.browsers = browsers
        self.contract = contract if contract is not None else renewal_contract.resolve()

    def system(self, business):
        return 'library-catalog'

    def bound(self, reader, record):
        require(self.browsers.sessions[reader.sid]['account_id'] == record['account'], 'ACTION_ACCOUNT_MISMATCH')

    def preview(self, args):
        require(c.enabled(), 'LIBRARY_WRITES_DISABLED')
        require(c.renewal_enabled(), 'LIBRARY_RENEW_DISABLED')
        with Reader(self.browsers) as reader:
            return loan_proposal(reader, args['record_id'], self.contract)[0]

    def preflight(self, record):
        require(c.renewal_enabled(), 'LIBRARY_RENEW_DISABLED')
        business = record['business']
        with Reader(self.browsers) as reader:
            self.bound(reader, record)
            fresh, _ = loan_proposal(reader, business['record_id'], self.contract)
        handoff = fresh['handoff'] or None
        requests = [] if handoff or self.contract is None else [
            transport.specification(self.contract, fresh['record_id'], fresh['due_at'])]
        return {'business': fresh, 'context': None, 'official_handoff': handoff,
                'checks': {'availability': True, 'eligibility': True, 'fees': True, 'rules': True},
                'requests': requests}

    def dispatch(self, record, gate):
        business = record['business']
        if not c.renewal_enabled() or self.contract is None:
            raise NotDispatched()
        with Reader(self.browsers) as reader:
            self.bound(reader, record)
            fresh, _ = loan_proposal(reader, business['record_id'], self.contract)
            if (fresh != record['business'] or fresh['handoff'] or
                    time.time() >= min(record['expires_at'],
                                       record['authorization']['evidence']['expires_at'])):
                raise NotDispatched()
            return transport.dispatch_exact(reader, self.contract, fresh['record_id'], fresh['due_at'], gate)

    def reconcile(self, record):
        business = record['business']
        with Reader(self.browsers) as reader:
            self.bound(reader, record)
            loans = read_loans(reader)
        matches = [row for row in loans if row['loan_id'] == business.get('record_id')]
        records = []
        if len(matches) == 1:
            due = matches[0].get('due_at')
            # Only a strictly later due time on the same record proves a renewal.
            if isinstance(due, str) and DUE.fullmatch(due) and isinstance(business.get('due_at'), str) \
                    and due > business['due_at']:
                records.append({
                    'service_id': 'library', 'account': record['account'], 'business': business,
                    'operation': c.RENEW_LOAN, 'record_id': business['record_id'], 'state': 'SUCCEEDED',
                    'platform': {'loan_id': business['record_id'], 'system': 'loans',
                                 'previous_due_at': business['due_at'], 'new_due_at': due,
                                 'renew_flag': matches[0].get('renew_flag'), 'official_url': OFFICIAL}})
        return {'complete': True, 'records': records}
