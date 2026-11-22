"""The plan validator is part of the offline check gate (P3-1)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_plan_validator_passes_on_the_current_tree():
    result = subprocess.run(
        [sys.executable, str(ROOT / 'plans' / 'v0.2.0' / '工具' / 'validate_plan.py')],
        cwd=ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace',
        timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report['ok'] is True
    assert report['modules'] == 24 and report['batches'] == 9


def test_dev_check_runs_the_plan_validator():
    text = (ROOT / 'scripts' / 'dev.py').read_text(encoding='utf-8')
    assert 'plans/v0.2.0/工具/validate_plan.py' in text
