"""Official ``triage -> ready``: a card that already carries scope skips the specifier.

``hermes kanban specify`` is an auxiliary-LLM rewrite. It is the right exit from
triage for a one-line idea and the wrong one for a PR card a receptor (or the
block-loop breaker) parked there with its measured body: the rewrite is what
would lose it. ``promote --from-triage`` is the audited, no-LLM exit, and it
fails closed — a card without scope is refused and pointed at ``specify``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_db import triage_scope_anchor

PR = 3177
URL = f"https://github.com/wyrep/wyrep/pull/{PR}"
HEAD = "b" * 40

SCOPED = (
    "**PR #3177 nasceu sem card de dono** (receptor do webhook).\n\n"
    "- Branch: `hermes/approve-dispatch-dedupe`\n"
    f"- PR: {URL}\n\n"
    "## O que fazer\n\n"
    "1. Leia o PR e o estado da fila: checks do HEAD atual, aprovacao valida, rotulo de fila.\n"
    "2. Registre no card o que mediu e nomeie o proximo passo do fluxo.\n"
)
THIN = "arrumar o login"


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _triage(conn, *, body: str = SCOPED, title: str = "[wyrep · pr] dono do PR",
            assignee: str = "wyai-hermes") -> str:
    return kb.create_task(conn, title=title, body=body, assignee=assignee, triage=True)


def _marker(resume: str = "promote") -> str:
    return f"decisao=fila-saiu pr={PR} head={HEAD} causa=success resume={resume}"


def _events(conn, task_id: str, kind: str) -> list[dict]:
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
        (task_id, kind),
    ).fetchall()
    return [json.loads(row["payload"]) for row in rows if row["payload"]]


def _status(conn, task_id: str) -> str:
    return conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()["status"]


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

def test_triage_without_the_flag_is_refused_and_names_it(kanban_home) -> None:
    with kbc.connect() as conn:
        tid = _triage(conn)
        ok, err = kb.promote_task(conn, tid, actor="me")
        assert ok is False
        assert "--from-triage" in (err or "") and "specify" in (err or "")
        assert _status(conn, tid) == "triage"


def test_triage_without_scope_is_refused_even_with_the_flag(kanban_home) -> None:
    with kbc.connect() as conn:
        tid = _triage(conn, body=THIN, title="arrumar o login")
        ok, err = kb.promote_task(conn, tid, actor="me", from_triage=True)
        assert ok is False
        assert "specify" in (err or "")
        assert _status(conn, tid) == "triage"
        assert _events(conn, tid, "promoted_manual") == []


def test_long_body_without_a_concrete_anchor_is_refused(kanban_home) -> None:
    with kbc.connect() as conn:
        tid = _triage(conn, body="resolver aquele problema do fluxo de aprovacao " * 8)
        ok, err = kb.promote_task(conn, tid, actor="me", from_triage=True)
        assert ok is False and "scope" in (err or "")
        assert _status(conn, tid) == "triage"


def test_triage_with_scope_is_released_to_ready_and_audited(kanban_home) -> None:
    with kbc.connect() as conn:
        tid = _triage(conn)
        ok, err = kb.promote_task(conn, tid, actor="csf", from_triage=True,
                                  reason=_marker())
        assert ok is True, err
        assert _status(conn, tid) == "ready"
        events = _events(conn, tid, "promoted_manual")
        assert len(events) == 1
        assert events[0]["actor"] == "csf"
        assert events[0]["from"] == "triage"
        assert events[0]["scope"] == URL
        assert events[0]["reason"] == _marker()


def test_released_card_is_not_held_by_the_pr_guard(kanban_home) -> None:
    """The whole point: the card is back in the queue AND spawnable."""
    with kbc.connect() as conn:
        tid = _triage(conn)
        kb.add_comment(conn, tid, author="receptor", body=f"PR: {URL}")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_comments SET created_at = created_at - 120 WHERE task_id = ?",
                (tid,),
            )
        # The hold is about the card, not its column: a triage card whose newest
        # comment names an OPEN PR is held too.
        assert kbd.check_respawn_guard(conn, tid, lane="ready") == "active_pr"
        ok, err = kb.promote_task(conn, tid, actor="csf", from_triage=True)
        assert ok is True, err
        assert kbd.check_respawn_guard(conn, tid, lane="ready") is None


def test_release_gated_on_a_path_does_not_lift_the_pr_hold(kanban_home) -> None:
    """Only a release whose scope names THIS PR unarms the guard."""
    path_body = (
        "o arquivo /opt/wyrep-wt/cockpit/scripts/ci/wyrep_pr_card.py guarda o receptor "
        "e a chamada dele, com o card e o PR nomeados no corpo do card. " * 3
    )
    with kbc.connect() as conn:
        tid = _triage(conn, body=path_body)
        kb.add_comment(conn, tid, author="receptor", body=f"PR: {URL}")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_comments SET created_at = created_at - 120 WHERE task_id = ?",
                (tid,),
            )
        ok, err = kb.promote_task(conn, tid, actor="csf", from_triage=True)
        assert ok is True, err
        assert _status(conn, tid) == "ready"
        assert kbd.check_respawn_guard(conn, tid, lane="ready") == "active_pr"


def test_dry_run_validates_without_mutating(kanban_home) -> None:
    with kbc.connect() as conn:
        tid = _triage(conn)
        ok, err = kb.promote_task(conn, tid, actor="me", from_triage=True, dry_run=True)
        assert ok is True and err is None
        assert _status(conn, tid) == "triage"
        assert _events(conn, tid, "promoted_manual") == []


def test_unfinished_parent_still_gates_a_triage_release(kanban_home) -> None:
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="pai", assignee="dev")
        tid = kb.create_task(conn, title="filho", body=SCOPED, assignee="dev",
                             parents=(parent,), triage=True)
        ok, err = kb.promote_task(conn, tid, actor="me", from_triage=True)
        assert ok is False and "parent" in (err or "")
        assert _status(conn, tid) == "triage"


def test_blocked_still_promotes_without_scope(kanban_home) -> None:
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="concluir algo", assignee="dev",
                             initial_status="blocked")
        assert _status(conn, tid) == "blocked"
        ok, err = kb.promote_task(conn, tid, actor="me")
        assert ok is True, err
        assert _status(conn, tid) == "ready"
        assert "from" not in _events(conn, tid, "promoted_manual")[0]


def test_ready_card_is_still_refused(kanban_home) -> None:
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="ja na fila", assignee="dev")
        ok, err = kb.promote_task(conn, tid, actor="me", from_triage=True)
        assert ok is False and "only applies" in (err or "")


# ---------------------------------------------------------------------------
# The anchor itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("body,expected", [
    (f"O PR esta aberto e o card ficou sem dono. PR: {URL}\n\nLeia os checks do "
     f"HEAD atual, confira a aprovacao e registre no card o que mediu. " * 2, URL),
    ("PR #3151 ficou sem card de dono, e o resto do corpo tem tamanho suficiente "
     "para nao ser um one-liner: leia o PR, meça os checks e registre. " * 2, "#3151"),
    ("rode `gh pr checks 3151` e registre no card o que mediu, com data e comando, "
     "sem inventar numero nenhum. " * 2, "`gh pr checks 3151`"),
    ("o arquivo /opt/wyrep-wt/cockpit/scripts/ci/wyrep_pr_card.py guarda o receptor "
     "e a chamada dele, com o card e o PR nomeados no corpo do card. " * 2,
     "/opt/wyrep-wt/cockpit/scripts/ci/wyrep_pr_card.py"),
    ("PLAN-CI-2026-11-08-DONO-MERGE-CONFIRMADO e o plano deste trabalho, com os "
     "passos medidos e o criterio de pronto escrito nele. " * 2,
     "PLAN-CI-2026-11-08-DONO-MERGE-CONFIRMADO"),
])
def test_anchor_accepts_the_forms_a_real_card_carries(body: str, expected: str) -> None:
    assert triage_scope_anchor("titulo", body) == expected


def test_anchor_refuses_thin_and_vague_bodies() -> None:
    assert triage_scope_anchor("t", THIN) is None
    assert triage_scope_anchor("t", "resolver aquilo " * 30) is None
    assert triage_scope_anchor("t", "") is None
