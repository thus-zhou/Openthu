"""Own-loan renewal: eligibility, one bound request and new-due readback.

Only the read side and one explicitly contracted request live here.  With no
verified source contract the plan carries ``RENEWAL_SOURCE_CONTRACT_UNVERIFIED``
and is handed to the official MyLib page; nothing is POSTed and no local state
claims that a loan was renewed.  A ``SYNTHETIC`` contract is not executable
unless the adapter was constructed with the explicit ``allow_synthetic=True``
test opt-in.  Success requires the same loan record to come back from a
complete own-loan read with a strictly later due time.
"""
from datetime import date, datetime, time as clock_time, timedelta, timezone
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
TZ = timezone(timedelta(hours=8))
DUE = re.compile(r'\d{4}-\d{2}-\d{2}(?:T(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?(?:Z|[+-]\d{2}:\d{2})?)?')
HINTS = 5


def require(acknowledged, code):
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
    """The official table and the JSON rows must describe the same page.

    Titles are compared by a short prefix: the rendered cell may truncate a long
    title, and blocking preparation on a cosmetic truncation would make the whole
    renewal path unavailable.
    """
    body = page.locator('.el-table__body-wrapper')
    observed = body.inner_text() if body.count() == 1 else ''
    marker = page.locator('.el-table__empty-text')
    empty = any(text.strip() in ('暂无数据', 'No Data', 'No data') for text in marker.all_text_contents())
    return all(str(row.get('title') or '')[:10] in observed for row in rows) if rows else empty


def read_loans(reader):
    """Read every active own loan; a partial list is never used for a write."""
    items, seen = [], set()
    for offset in range(1, 1002, 10):
        try:
            rows, more = lib.parse_loans(reader.page.evaluate(lib.LOAN_FETCH, offset))
            if offset == 1:
                require(_page_agrees(reader.page, rows), 'RENEWAL_PAGE_DISAGREES_WITH_API')
        except CampusError:
            raise
        except Exception:
            raise CampusError('LIBRARY_LOAN_PAGE_FAILED') from None
        current = {row['loan_id'] for row in rows}
        require(len(current) == len(rows) and not seen.intersection(current), 'RENEWAL_LOAN_PAGE_REPEATED')
        seen.update(current)
        items.extend(rows)
        if not more:
            return items
        require(len(rows) == 10, 'RENEWAL_LOAN_PAGINATION_UNVERIFIED')
    raise CampusError('RENEWAL_LOAN_LIST_LIMIT')


def due_value(value):
    """Parse a due value into (local date, local time or None for date-only)."""
    if not isinstance(value, str) or not DUE.fullmatch(value):
        return None
    try:
        if len(value) == 10:
            return date.fromisoformat(value), None
        parsed = datetime.fromisoformat(value[:-1] + '+00:00' if value.endswith('Z') else value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    local = parsed.astimezone(TZ)
    return local.date(), local.time().replace(second=0, microsecond=0)


def due_later(new_value, old_value):
    """True only for a strictly later instant; a same-day precision change is not."""
    new, old = due_value(new_value), due_value(old_value)
    if new is None or old is None:
        return False
    if new[0] != old[0]:
        return new[0] > old[0]
    if new[1] is None or old[1] is None:
        return False
    return new[1] > old[1]


def _summary(loan, built, official):
    block = next((item for item in built['items']
                  if item['resource']['resource_id'] == loan['loan_id']), None)
    states = c.precheck_states(block or {})
    states['selection_state'] = 'UNIQUE'
    if loan.get('renewal_state') == 'NOT_ELIGIBLE' or loan.get('overdue') is True:
        states['preparation_state'] = 'NOT_PREPARABLE'
    elif not official:
        states['preparation_state'] = 'NEEDS_OFFICIAL_PAGE'
    elif loan.get('renewal_state') == 'ELIGIBLE':
        states['preparation_state'] = 'NEEDS_ELIGIBILITY_CHECK'
    else:
        states['preparation_state'] = 'UNKNOWN'
    return states


def loan_proposal(reader, loan_id, contract, allow_synthetic=False):
    """Bind exactly one own loan; read-only unless a usable contract was supplied."""
    loans = read_loans(reader)
    built = precheck.build('library', 'loans', {'url': OFFICIAL, 'complete': True}, loans, {}, None)
    matches = [row for row in loans if row['loan_id'] == loan_id]
    require(matches, 'RENEWAL_RECORD_NOT_FOUND')
    require(len(matches) == 1, 'RENEWAL_RECORD_NOT_UNIQUE')
    loan = matches[0]
    due = loan.get('due_at')
    require(due_value(due) is not None, 'LIBRARY_LOAN_DATE_INVALID')
    state = loan.get('renewal_state')
    if loan.get('overdue') is True:
        raise CampusError('RENEWAL_OVERDUE')
    if state == 'NOT_ELIGIBLE':
        raise CampusError('RENEWAL_NOT_ELIGIBLE')
    if contract is None:
        contract_id, contract_digest, contract_evidence, usable = '', '', 'NONE', False
    else:
        usable = contract.usable(allow_synthetic)
        contract_id, contract_digest, contract_evidence = (
            contract.contract_id, contract.digest, contract.evidence_state)
    flags = []
    if state != 'ELIGIBLE':
        flags.append('RENEWAL_ELIGIBILITY_UNVERIFIED')
    if contract is None:
        flags.append('RENEWAL_SOURCE_CONTRACT_UNVERIFIED')
    elif not usable:
        flags.append('RENEWAL_CONTRACT_EVIDENCE_UNVERIFIED')
    hints = loan.get('renewal_hints') or []
    business = {
        'system': 'loans', 'date': due[:10], 'subject': loan.get('title') or '',
        'target': '图书馆借阅续借（当前到期 ' + due + '）',
        'record_id': loan['loan_id'], 'due_at': due,
        'renew_flag': loan.get('renew_flag') or '', 'renewal_state': state or 'UNKNOWN',
        'renewal_hint': '；'.join(str(item) for item in hints[:HINTS])[:300],
        'official_url': OFFICIAL, 'handoff': ','.join(flags),
        'rules_summary': '续借规则以官方 MyLib 页面为准；本计划只核对本人借阅记录的新到期时间。',
        'precheck_summary': _summary(loan, built, usable),
        'contract_id': contract_id, 'contract_digest': contract_digest,
        'contract_evidence': contract_evidence, 'reader_account': reader.account,
        'proof': digest([loan['loan_id'], due, loan.get('renew_flag'), hints, reader.account]),
        'source': 'library.renewal'}
    return business, loan


class RenewalAction:
    """Registered renewal adapter; execution needs an explicitly usable contract."""

    version = c.VERSION
    accepts_ledger = False

    def __init__(self, browsers, contract=None, allow_synthetic=False):
        self.browsers = browsers
        self.contract = contract if contract is not None else renewal_contract.resolve()
        # Explicit code-level opt-in used only by synthetic tests.  It is not an
        # environment variable, tool argument or model output.
        self.allow_synthetic = allow_synthetic is True

    def system(self, business):
        return 'library-catalog'

    def bound(self, reader, record):
        require(self.browsers.sessions[reader.sid]['account_id'] == record['account'], 'ACTION_ACCOUNT_MISMATCH')

    def check_account(self, reader, business):
        """The bound Primo account must be the same account that prepared the plan."""
        require(reader.account == business.get('reader_account'), 'RENEWAL_ACCOUNT_CHANGED_PREPARE_AGAIN')

    def usable(self, contract):
        return contract is not None and contract.usable(self.allow_synthetic)

    def preview(self, args):
        require(c.enabled(), 'LIBRARY_WRITES_DISABLED')
        require(c.renewal_enabled(), 'LIBRARY_RENEW_DISABLED')
        try:
            with Reader(self.browsers) as reader:
                return loan_proposal(reader, args['record_id'], self.contract, self.allow_synthetic)[0]
        except CampusError:
            raise
        except Exception:
            raise CampusError('RENEWAL_PREVIEW_FAILED') from None

    def preflight(self, record):
        require(c.renewal_enabled(), 'LIBRARY_RENEW_DISABLED')
        business = record['business']
        with Reader(self.browsers) as reader:
            self.bound(reader, record)
            self.check_account(reader, business)
            fresh, _ = loan_proposal(reader, business['record_id'], self.contract, self.allow_synthetic)
        handoff = fresh['handoff'] or None
        requests = [] if handoff or not self.usable(self.contract) else [
            transport.specification(self.contract, fresh['record_id'], fresh['due_at'])]
        states = fresh['precheck_summary']
        return {'business': fresh, 'context': None, 'official_handoff': handoff,
                'checks': {'eligibility': states['eligibility_state'] == 'ELIGIBLE',
                           'rules': states['rules_state'] == 'READ'},
                'requests': requests}

    def dispatch(self, record, gate):
        business = record['business']
        if not c.renewal_enabled() or not self.usable(self.contract) or \
                self.contract.digest != business.get('contract_digest'):
            raise NotDispatched()
        with Reader(self.browsers) as reader:
            self.bound(reader, record)
            try:
                self.check_account(reader, business)
            except CampusError:
                raise NotDispatched() from None
            fresh, _ = loan_proposal(reader, business['record_id'], self.contract, self.allow_synthetic)
            if (fresh != record['business'] or fresh['handoff'] or
                    time.time() >= min(record['expires_at'],
                                       record['authorization']['evidence']['expires_at'])):
                raise NotDispatched()
            return transport.dispatch_exact(reader, self.contract, fresh['record_id'], fresh['due_at'],
                                            gate, self.allow_synthetic)

    def reconcile(self, record):
        business = record['business']
        with Reader(self.browsers) as reader:
            self.bound(reader, record)
            self.check_account(reader, business)
            loans = read_loans(reader)
        matches = [row for row in loans if row['loan_id'] == business.get('record_id')]
        records = []
        if len(matches) == 1 and due_later(matches[0].get('due_at'), business.get('due_at')):
            records.append({
                'service_id': 'library', 'account': record['account'], 'business': business,
                'operation': c.RENEW_LOAN, 'record_id': business['record_id'], 'state': 'SUCCEEDED',
                'platform': {'loan_id': business['record_id'], 'system': 'loans',
                             'previous_due_at': business['due_at'], 'new_due_at': matches[0].get('due_at'),
                             'renew_flag': matches[0].get('renew_flag'), 'official_url': OFFICIAL,
                             # Whether this adapter sent a request or the user
                             # completed the renewal on the official page is not
                             # provable; say so instead of claiming our write.
                             'completed_by': ('OFFICIAL_HANDOFF' if record.get('state') == 'AWAITING_OFFICIAL'
                                              else 'ADAPTER_DISPATCH_OR_OFFICIAL_PAGE')}})
        return {'complete': True, 'records': records}
