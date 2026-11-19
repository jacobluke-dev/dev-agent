"""Protect compact terminal output, evidence links and compatibility with older records."""

import io
import json
from pathlib import Path

import pytest

from dev_agent.records import Store
from dev_agent.reporting.terminal import render_run, state


class Terminal(io.StringIO):
    """Model a text stream whose terminal capability is selectable."""

    def __init__(self, *, tty: bool) -> None:
        """Store the capability for the renderer's isatty check."""
        super().__init__()
        self.tty = tty

    def isatty(self) -> bool:
        """Return the simulated capability."""
        return self.tty


@pytest.mark.parametrize(
    ("tty", "no_color", "term", "coloured"),
    [
        (True, False, "xterm", True),
        (False, False, "xterm", False),
        (True, True, "xterm", False),
        (True, False, "dumb", False),
    ],
)
def test_status_uses_colour_only_when_supported(
    tty: bool,
    no_color: bool,
    term: str,
    coloured: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep labels meaningful when stdout is redirected or colour is disabled."""
    monkeypatch.setenv("TERM", term)
    if no_color:
        monkeypatch.setenv("NO_COLOR", "1")
    else:
        monkeypatch.delenv("NO_COLOR", raising=False)
    status = state("passed", Terminal(tty=tty))
    assert "[PASS]" in status
    assert ("\x1b[" in status) == coloured


def test_sparse_historical_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Display an older record without assuming newer audit or verification fields exist."""
    store = Store(tmp_path / "runs")
    render_run(store, {"schema_version": 1, "run_id": "fixture-1", "outcome": "passed"}, "runs")
    plain = capsys.readouterr().out
    assert "fixture-1" in plain
    assert "Review       [NOT RUN] not performed" in plain
    assert plain.count("[NOT RUN]") == 7
    assert "Record" not in plain
    assert not store.root.exists()


def test_delivery_uses_linked_stage_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Show the parent's blocker with the latest recorded state of each child stage."""
    store = Store(tmp_path / "runs")
    child = {
        "run_id": "child-1",
        "schema_version": 2,
        "instruction_compliance": {"status": "changed_scopes_validated"},
        "verification_attempts": [
            {"profile": "targeted", "status": "failed", "attempt_id": "historical-secret"},
            {"profile": "targeted", "status": "passed", "preparation": {"status": "reused"}},
            {"profile": "full", "status": "failed"},
        ],
        "independent_review": {"status": "pending"},
    }
    store.create(child)
    saved = store.directory("child-1") / "record.json"
    before = saved.read_bytes()
    render_run(
        store,
        {
            "project": "example",
            "task": "TASK-1",
            "run_id": "delivery-1",
            "kind": "delivery",
            "implementation_run_id": "child-1",
            "phase": "finished",
            "events": [{"phase": "verifying_full"}],
            "outcome": "verification_blocked",
            "blockers": ["Full verification failed\n\x1b[31m"],
        },
        "deliver",
    )
    plain = capsys.readouterr().out
    for expected in (
        "example / TASK-1",
        "delivery-1",
        "verifying full",
        "[BLOCKED] verification blocked",
        "Preparation  [PASS]",
        "Instructions [PASS]",
        "Targeted     [PASS]",
        "Full         [FAIL]",
        "Review       [PENDING]",
        "GitHub CI    [NOT RUN]",
        "Full verification failed",
        "Next",
        str(saved),
    ):
        assert expected in plain
    assert "\x1b" not in plain and "historical-secret" not in plain
    assert saved.read_bytes() == before


@pytest.mark.parametrize(
    ("status", "verdict", "badge", "blocker"),
    [
        (
            "review_blocked",
            "unable_to_verify",
            "[BLOCKED] unable to verify",
            "could not establish correctness",
        ),
        ("review_blocked", "changes_requested", "[FAIL] changes requested", "requested changes"),
        ("failed", None, "[FAIL] review execution failed", "Agent timed out"),
        ("security_failed", "passed", "[FAIL] security failed", "Snapshot changed"),
        ("review_blocked", None, "[BLOCKED] missing review evidence", "evidence is incomplete"),
        (None, None, "[BLOCKED] missing review evidence", "evidence is incomplete"),
    ],
)
def test_review_blocker(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    status: str | None,
    verdict: str | None,
    badge: str,
    blocker: str,
) -> None:
    """Explain blocked adoption from the attempt, including failures after a verdict."""
    record = {"kind": "adoption", "adoption": {"status": "review_blocked"}}
    if status:
        record["independent_review"] = {
            "status": status,
            "verdict": verdict,
            "error": {"failed": "Agent timed out", "security_failed": "Snapshot changed"}.get(
                status
            ),
        }
    render_run(Store(tmp_path), record, "adopt")
    output = capsys.readouterr().out
    assert "Stage        Independent review\n" in output
    assert f"Review       {badge}\n" in output
    assert blocker in next(line for line in output.splitlines() if line.startswith("Blocker"))
    assert "Next " in output
    assert "review blocked" not in output


@pytest.mark.parametrize(
    ("verdict", "specific", "label"),
    [
        (
            "unable_to_verify",
            {"criteria": [{"status": "unable_to_verify", "explanation": "Fixture unavailable."}]},
            "[BLOCKED] unable to verify",
        ),
        (
            "changes_requested",
            {
                "findings": [
                    {"severity": "blocking", "explanation": "Return value violates contract."}
                ]
            },
            "[FAIL] changes requested",
        ),
        ("passed", {}, "[PASS] passed"),
    ],
)
def test_linked_review_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    verdict: str,
    specific: dict,
    label: str,
) -> None:
    """Show the latest child attempt's report and most specific finding without editing evidence."""
    store = Store(tmp_path)
    passed = verdict == "passed"
    directory = store.create(
        {
            "run_id": "child",
            "schema_version": 2,
            "independent_review": {
                "attempt_id": "latest",
                "status": "passed" if passed else "review_blocked",
                "verdict": verdict,
            },
        }
    )
    report = directory / "review/latest/review.json"
    report.parent.mkdir(parents=True)
    body = json.dumps({"verdict": verdict, "summary": "General assessment", **specific})
    report.write_text(body)
    render_run(
        store,
        {
            "kind": "delivery",
            "implementation_run_id": "child",
            "outcome": "delivered" if passed else "review_blocked",
        },
        "deliver",
    )
    output = capsys.readouterr().out
    assert f"Review       {label}" in output
    assert str(report) in output
    if specific:
        assert next(iter(specific.values()))[0]["explanation"] in output
        assert "General assessment" not in output
    else:
        assert "Blocker" not in output
    assert report.read_text() == body
