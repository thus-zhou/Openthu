"""Explicit test-only journal codec; never imported by production modules.

Windows fixtures exercise actual DPAPI. Other hosts use a reversible codec for
synthetic records, while production defaults continue to reject encryption there.
This codec provides no security and must never receive real account information.
"""
import os
from thu_assistant.action_store import ActionStore

PREFIX=b'OPENTHU-SYNTHETIC-ONLY\0'


def wrap(data):
    return PREFIX+bytes(value^90 for value in data)


def unwrap(data):
    if not data.startswith(PREFIX):
        raise ValueError('Not a synthetic test journal')
    return bytes(value^90 for value in data[len(PREFIX):])


def make_action_store(root):
    if os.name=='nt':
        return ActionStore(root)
    return ActionStore(root,protect=wrap,unprotect=unwrap)
