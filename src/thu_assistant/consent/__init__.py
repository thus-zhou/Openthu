"""Trusted-host authorization for campus actions.

Public surface:

* ``VERSION`` / ``new_challenge`` - protocol identifier and plan challenge.
* ``HostConsent`` - protected host key, pending grant storage, one-time take.
* ``issue`` / ``verify`` - pure signed-grant protocol functions.
* ``authorize`` - local interactive confirmation used by the CLI.
"""
from .cli import authorize
from .protocol import VERSION, canonical, issue, new_challenge, sign, verify
from .store import HostConsent


class _Decision:
    """A trusted confirmation callback decision that is not a grant."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return "consent." + self.name


#: The human explicitly refused this transaction.  A pending signed grant must
#: not override the refusal.
DECLINED = _Decision("DECLINED")

#: The confirmation path does not apply on this host; a pending signed grant
#: may still be used.
NOT_APPLICABLE = _Decision("NOT_APPLICABLE")

__all__ = [
    "DECLINED",
    "NOT_APPLICABLE",
    "VERSION",
    "HostConsent",
    "authorize",
    "canonical",
    "issue",
    "new_challenge",
    "sign",
    "verify",
]
