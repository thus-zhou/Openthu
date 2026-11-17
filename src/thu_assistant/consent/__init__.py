"""Trusted-host authorization for campus actions.

Public surface:

* ``VERSION`` / ``new_challenge`` - protocol identifier and plan challenge.
* ``HostConsent`` - protected host key, pending grant storage, one-time take.
* ``issue`` / ``verify`` - pure signed-grant protocol functions.
* ``authorize`` - local interactive confirmation used by the CLI.
"""
from .cli import authorize
from .protocol import VERSION, canonical, fingerprint, issue, new_challenge, sign, verify
from .store import HostConsent

__all__ = [
    "VERSION",
    "HostConsent",
    "authorize",
    "canonical",
    "fingerprint",
    "issue",
    "new_challenge",
    "sign",
    "verify",
]
