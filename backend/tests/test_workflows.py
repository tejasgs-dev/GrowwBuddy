"""The daily refresh workflow file (tasks 7.2, 7.9, 7.10).

CI's `workflows` job runs actionlint on every workflow; this test also runs it when the
binary is on PATH (or named by $ACTIONLINT), and checks the settings the plan fixes.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
REFRESH = WORKFLOWS / "refresh-data.yml"


@pytest.fixture(scope="module")
def wf() -> dict[Any, Any]:
    data: dict[Any, Any] = yaml.safe_load(REFRESH.read_text(encoding="utf-8"))
    return data


def test_runs_daily_at_10_ist_and_by_hand(wf: dict[Any, Any]) -> None:
    on = wf[True]  # YAML 1.1 reads the `on:` key as a boolean
    assert on["schedule"] == [{"cron": "30 4 * * *"}]  # 04:30 UTC = 10:00 IST
    assert "workflow_dispatch" in on


def test_job_limits_and_permissions(wf: dict[Any, Any]) -> None:
    assert wf["concurrency"]["group"] == "refresh-data"
    assert wf["permissions"] == {"contents": "write", "issues": "write"}
    job = wf["jobs"]["refresh"]
    assert job["timeout-minutes"] == 20
    assert job["runs-on"] == "ubuntu-latest"


def test_every_action_is_pinned_to_a_commit(wf: dict[Any, Any]) -> None:
    uses = [s["uses"] for s in wf["jobs"]["refresh"]["steps"] if "uses" in s]
    assert uses
    for ref in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), ref


def test_no_groq_key_in_the_workflow() -> None:
    """Decision (task 7.9): no GROQ_API_KEY secret; unknown exit-load wording is stored
    unverified and listed in the review issue."""
    text = REFRESH.read_text(encoding="utf-8")
    assert "secrets.GROQ_API_KEY" not in text
    assert "needs_review" in text


def test_actionlint_passes() -> None:
    exe = os.environ.get("ACTIONLINT") or shutil.which("actionlint")
    if not exe:
        pytest.skip("actionlint not installed (CI's workflows job runs it)")
    proc = subprocess.run(
        [exe, str(REFRESH), str(WORKFLOWS / "ci.yml")],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
