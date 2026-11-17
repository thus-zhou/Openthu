"""Frozen renewal source contracts.

The production registry is deliberately empty: no verified renewal *request*
contract exists yet (the source registry marks ``library.renewal`` as PARTIAL --
eligibility hints are readable, execution is not).  A contract object is therefore
only usable when it is explicitly injected by a composition root or a test, so a
guessed endpoint can never be reached from model output, environment variables or
ordinary tool arguments.

``evidence_state`` keeps a synthetic fixture distinguishable from a verified
source.  A synthetic contract is never part of the production default path.
"""
from dataclasses import dataclass
import re

from ..bridge import CampusError

SYNTHETIC = 'SYNTHETIC'
VERIFIED = 'VERIFIED'
STATES = (SYNTHETIC, VERIFIED)
_PATH = re.compile(r'/[A-Za-z0-9_./-]{1,120}')
_BODY_FIELDS = ('loan_id', 'due_at')


@dataclass(frozen=True)
class SourceContract:
    """One exact renewal request shape, only usable when explicitly supplied."""

    contract_id: str
    origin: str
    path: str
    encoding: str = 'json'
    success_field: str = 'code'
    success_value: int = 0
    evidence_state: str = SYNTHETIC
    note: str = ''

    def __post_init__(self):
        if not isinstance(self.contract_id, str) or not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,60}', self.contract_id):
            raise CampusError('RENEWAL_CONTRACT_INVALID')
        if self.evidence_state not in STATES:
            raise CampusError('RENEWAL_CONTRACT_INVALID')
        if self.encoding not in ('json', 'form'):
            raise CampusError('RENEWAL_CONTRACT_INVALID')
        if not isinstance(self.path, str) or not _PATH.fullmatch(self.path):
            raise CampusError('RENEWAL_CONTRACT_INVALID')
        if not isinstance(self.success_field, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.]{0,60}', self.success_field):
            raise CampusError('RENEWAL_CONTRACT_INVALID')
        if type(self.success_value) is not int:
            raise CampusError('RENEWAL_CONTRACT_INVALID')
        from ..browser import validate_url
        validate_url(self.origin + self.path)

    @property
    def url(self):
        return self.origin + self.path

    def body(self, loan_id, due_at):
        """Canonical fields; a verified contract's exact mapping lands in 2.3."""
        return {_BODY_FIELDS[0]: loan_id, _BODY_FIELDS[1]: due_at}

    def accepted(self, payload):
        if not isinstance(payload, dict):
            return False
        node = payload
        for part in self.success_field.split('.'):
            if not isinstance(node, dict) or part not in node:
                return False
            node = node[part]
        return node == self.success_value and not isinstance(node, bool)


# Production default.  A verified contract must be frozen through 2.3/1.1
# before this tuple becomes non-empty; modules/tests pass an explicit object.
REGISTERED: tuple[SourceContract, ...] = ()


def registered():
    return REGISTERED


def state():
    return 'REGISTERED' if REGISTERED else 'UNVERIFIED'


def resolve(contract_id=None):
    """Return the single registered contract, or None; never invent one."""
    if contract_id is None:
        return REGISTERED[0] if len(REGISTERED) == 1 else None
    matches = [item for item in REGISTERED if item.contract_id == contract_id]
    return matches[0] if len(matches) == 1 else None
