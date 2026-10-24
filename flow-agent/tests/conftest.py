"""Make the package importable from the test suite.

Inserts the project root into sys.path so tests can import `flow_agent` and `main` cleanly.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def no_humanize_waits(monkeypatch):
    # Human-like pacing is exercised in test_humanize.py with short samples;
    # every other suite runs with the waits disabled.
    monkeypatch.setattr('flow_agent.humanize.PACE', 0)
    # Scheduler spacing is covered in test_humanize.py; dispatch stays immediate.
    monkeypatch.setattr('flow_agent.task_scheduler.DEFAULT_MIN_INTERVAL', 0)
