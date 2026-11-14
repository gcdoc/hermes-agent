"""The rework-handoff contract: what may lift the ``active_pr`` respawn hold.

The contract lives in one module (``hermes_cli.kanban_db_rework``) and the
dispatcher's guard consumes it instead of carrying a second copy. These tests
pin both halves: the marker parser's closed lists, and the live guard's answer —
a ``done -> ready`` reanimation is worthless if the guard then holds the card
for 24h.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_db_rework import (
    _REWORK_CAUSES,
    _REWORK_DECISIONS,
    active_pr_engaged,
    handoff_lifts_active_pr,
    pr_numbers,
    unblocked_payload,
)

PR = 3177
REPO = "wyrep/wyrep"
URL = f"https://github.com/{REPO}/pull/{PR}"
HEAD = "a" * 40


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _marker(kind: str, *, pr: int = PR, head: str = HEAD,
            decisao: str = "ci-vermelho", causa: str = "failure") -> str:
    resume = {"unblocked": "unblock", "promoted_manual": "promote"}[kind]
    return f"decisao={decisao} pr={pr} head={head} causa={causa} resume={resume}"


def _payload(reason: str, **extra: object) -> str:
    return json.dumps({"reason": reason, **extra})


# ---------------------------------------------------------------------------
# The marker parser
# ---------------------------------------------------------------------------

def test_marker_lifts_on_its_own_kind_only() -> None:
    assert handoff_lifts_active_pr(
        "unblocked", _payload(_marker("unblocked")), frozenset({PR})) is True
    assert handoff_lifts_active_pr(
        "promoted_manual", _payload(_marker("promoted_manual")), frozenset({PR})) is True
    # An unblock cannot authorise a promotion and vice versa.
    assert handoff_lifts_active_pr(
        "unblocked", _payload(_marker("promoted_manual")), frozenset({PR})) is False
    assert handoff_lifts_active_pr(
        "promoted_manual", _payload(_marker("unblocked")), frozenset({PR})) is False


def test_marker_for_another_or_ambiguous_pr_lifts_nothing() -> None:
    marker = _payload(_marker("unblocked"))
    assert handoff_lifts_active_pr("unblocked", marker, frozenset({PR + 1})) is False
    assert handoff_lifts_active_pr("unblocked", marker, frozenset({PR, PR + 1})) is False
    assert handoff_lifts_active_pr("unblocked", marker, frozenset()) is False


@pytest.mark.parametrize("reason", [
    "",
    "no marker at all",
    f"decisao=ci-vermelho pr={PR} head=abc causa=failure resume=unblock",          # not a SHA
    f"decisao=ci-vermelho pr={PR} head={HEAD} causa=whatever resume=unblock",     # causa open
    f"decisao=inventada pr={PR} head={HEAD} causa=failure resume=unblock",        # decisao open
    f"decisao=ci-vermelho pr={PR} pr={PR} head={HEAD} causa=failure resume=unblock",  # duplicated key
])
def test_incomplete_or_open_ended_markers_keep_the_guard(reason: str) -> None:
    assert handoff_lifts_active_pr("unblocked", _payload(reason), frozenset({PR})) is False


def test_payload_that_is_not_an_object_with_reason_is_not_authority() -> None:
    assert handoff_lifts_active_pr("unblocked", None, frozenset({PR})) is False
    assert handoff_lifts_active_pr("unblocked", json.dumps("decisao=x"), frozenset({PR})) is False
    assert handoff_lifts_active_pr("unblocked", json.dumps({"reason": 7}), frozenset({PR})) is False


def test_triage_release_payload_lifts_only_for_the_held_pr() -> None:
    """The kernel's own ``triage -> ready`` release (``from``/``scope`` payload)."""
    release = json.dumps({"actor": "csf", "from": "triage", "scope": URL, "reason": None})
    assert handoff_lifts_active_pr("promoted_manual", release, frozenset({PR})) is True
    assert handoff_lifts_active_pr("promoted_manual", release, frozenset({PR + 1})) is False
    # A release is a promotion, never an unblock.
    assert handoff_lifts_active_pr("unblocked", release, frozenset({PR})) is False


def test_triage_release_gated_on_a_path_lifts_nothing() -> None:
    release = json.dumps({"from": "triage", "scope": "/opt/wyrep-wt/x/y.py"})
    assert handoff_lifts_active_pr("promoted_manual", release, frozenset({PR})) is False


def test_plain_promotion_without_scope_is_not_a_release() -> None:
    assert handoff_lifts_active_pr(
        "promoted_manual", json.dumps({"actor": "me", "reason": None}), frozenset({PR})) is False


def test_unblocked_payload_keeps_the_historical_shape() -> None:
    assert unblocked_payload("ready", "ready", None) is None
    assert unblocked_payload("blocked", "review", None) == {
        "status": "blocked", "resume_status": "review",
    }
    assert unblocked_payload("ready", "ready", "r") == {"reason": "r"}


def test_pr_numbers_reads_only_github_pull_urls() -> None:
    assert pr_numbers(f"PR: {URL} e {URL}/files") == frozenset({PR})
    assert pr_numbers(f"https://github.com/{REPO}/issues/9") == frozenset()


def test_closed_lists_cover_the_markers_the_receptor_sends() -> None:
    assert {"ci-vermelho", "push-novo", "mudancas-pedidas",
            "aprovacao-dispensada", "fila-saiu"} <= _REWORK_DECISIONS
    assert {"failure", "success", "cancelled", "synchronize",
            "changes_requested"} <= _REWORK_CAUSES


# ---------------------------------------------------------------------------
# The live guard
# ---------------------------------------------------------------------------

def _owner(conn, *, pr: int = PR) -> str:
    """A ready card whose newest comment names its own PR (guard armed)."""
    tid = kb.create_task(conn, title="dono do PR", body="entrega", assignee="dev")
    kb.add_comment(conn, tid, author="receptor", body=f"PR: {URL}")
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_comments SET created_at = created_at - 120 WHERE task_id = ?",
            (tid,),
        )
    return tid


def _append(conn, task_id: str, kind: str, payload: dict) -> None:
    with kb.write_txn(conn):
        kb._append_event(conn, task_id, kind, payload)


def test_guard_holds_the_card_while_the_pr_comment_is_newest(kanban_home) -> None:
    with kbc.connect() as conn:
        tid = _owner(conn)
        assert kbd.check_respawn_guard(conn, tid, lane="ready") == "active_pr"
        assert active_pr_engaged(conn, tid, since=0, unreadable=0) is True


def test_reanimation_event_lifts_the_guard(kanban_home) -> None:
    with kbc.connect() as conn:
        tid = _owner(conn)
        _append(conn, tid, "unblocked", {"reason": _marker("unblocked")})
        assert kbd.check_respawn_guard(conn, tid, lane="ready") is None
        assert active_pr_engaged(conn, tid, since=0, unreadable=0) is False


def test_promotion_marker_lifts_the_guard_too(kanban_home) -> None:
    with kbc.connect() as conn:
        tid = _owner(conn)
        _append(conn, tid, "promoted_manual", {
            "reason": _marker("promoted_manual", decisao="fila-saiu", causa="mergify-queue"),
        })
        assert kbd.check_respawn_guard(conn, tid, lane="ready") is None


def test_fabricated_reason_does_not_lift_the_guard(kanban_home) -> None:
    with kbc.connect() as conn:
        tid = _owner(conn)
        _append(conn, tid, "unblocked", {"reason": "trabalhando nisso"})
        assert kbd.check_respawn_guard(conn, tid, lane="ready") == "active_pr"


def test_operator_handoff_still_lifts_the_guard(kanban_home) -> None:
    """The pre-existing reassign path must keep working (no regression)."""
    with kbc.connect() as conn:
        tid = _owner(conn)
        _append(conn, tid, "assigned", {"from": "dev", "assignee": "outro"})
        assert kbd.check_respawn_guard(conn, tid, lane="ready") is None


def test_no_op_reassign_does_not_lift_the_guard(kanban_home) -> None:
    """dev -> dev is not a handoff: that is the implementer who opened the PR."""
    with kbc.connect() as conn:
        tid = _owner(conn)
        _append(conn, tid, "assigned", {"from": "dev", "assignee": "dev"})
        assert kbd.check_respawn_guard(conn, tid, lane="ready") == "active_pr"


def test_card_without_a_pr_comment_is_not_held(kanban_home) -> None:
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="sem PR", assignee="dev")
        assert kbd.check_respawn_guard(conn, tid, lane="ready") is None
