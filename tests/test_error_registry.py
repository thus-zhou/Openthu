"""Regression guards for the frozen error registry and the tool surface."""
from __future__ import annotations

import glob
import json
import re
from pathlib import Path

from thu_assistant import source_state
from thu_assistant.facade.core import FROZEN_LEGACY_TOOL_NAMES

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / 'contracts' / 'v0.2'
LITERAL = re.compile(
    r"(?:CampusError|FacadeError|MediaError|ResourceError|SubmissionError|StoreError|"
    r"LocalApiError|ActionError|RenewalError|SportsError)\(\s*['\"]([A-Z][A-Z0-9_]+)['\"]")


def test_every_error_literal_is_registered():
    registered = set(source_state.ERROR_CODES)
    missing = {}
    for path in glob.glob(str(ROOT / 'src' / 'thu_assistant' / '**' / '*.py'), recursive=True):
        if '__pycache__' in path:
            continue
        text = Path(path).read_text(encoding='utf-8', errors='ignore')
        for code in LITERAL.findall(text):
            if code not in registered:
                missing.setdefault(code, set()).add(Path(path).name)
    assert missing == {}
    assert 'WRITES_DISABLED' in registered


def test_error_registry_mirror_matches_the_contract():
    frozen = json.loads((CONTRACTS / 'error-classification.json').read_text(encoding='utf-8'))
    assert source_state.ERROR_CODES == frozen['codes']


def test_frozen_tool_names_match_the_compatibility_baseline():
    baseline = json.loads((CONTRACTS / 'compatibility-baseline.json').read_text(encoding='utf-8'))
    assert tuple(baseline['mcp_tools']) == FROZEN_LEGACY_TOOL_NAMES
    assert baseline['mcp_tool_count'] == len(FROZEN_LEGACY_TOOL_NAMES) == 29
