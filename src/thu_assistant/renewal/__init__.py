"""Loan renewal: read eligibility, bind one own loan, reconcile a new due time.

Public entry points stay on ``library_contracts.renew_loan`` so the business
facade (6.1) can register this adapter without changing the three legacy library
operations.  No renewal request is dispatched unless a verified source contract
is explicitly supplied; the default path is an official-page handoff.
"""
from .adapter import OFFICIAL, RenewalAction, Reader, loan_proposal, read_loans
from .contract import REGISTERED, SourceContract, registered, resolve, state

__all__ = ['OFFICIAL', 'RenewalAction', 'Reader', 'loan_proposal', 'read_loans',
           'REGISTERED', 'SourceContract', 'registered', 'resolve', 'state']
