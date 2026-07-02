"""Tests for chain_coordinator — the chain-lifecycle bridge to project_coordinator.

Covers:
- Context manager register-on-enter / deregister-on-exit.
- Inactive mode when no project is passed (every method is a safe no-op).
- Background heartbeat thread lifecycle.
- Exception isolation: every underlying pc.* call can raise and the
  ChainCoordinatorContext method must log and return a sentinel, never
  propagate. This is the invariant that lets chain.py adopt the bridge
  without needing a try/except at every call site.
- Pass-through semantics for set_focus / claim_files / release_files /
  file_owner / append_note / project_summary / other_chains.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

_project_root = str(Path(__file__).parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import chain_coordinator as cc
import project_coordinator as pc


# --- Helpers ---------------------------------------------------------


def _wait_for(predicate, timeout: float = 2.0, poll: float = 0.01):
    """Block until predicate() is truthy or timeout elapses. Test helper.

    Avoids the `sleep(n); assert` anti-pattern that flakes on slow CI.
    Returns the predicate's final value so callers can assert on it.
    """
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(poll)
    return last


# --- Activation ------------------------------------------------------


def test_inactive_when_project_dir_is_none(tmp_path):
    """project_dir=None → inactive context; nothing touches disk."""
    c = cc.ChainCoordinatorContext(None, "c1", focus="x")
    assert c.active is False
    assert c.project_dir is None
    with c as coord:
        # Register didn't run → coordination dir was never created.
        assert not (tmp_path / ".dialectic").exists()
        assert coord.heartbeat() is False
        assert coord.set_focus("new") is False
        assert coord.claim_files(["a.py"]) == (False, [])
        assert coord.release_files() == 0
        assert coord.file_owner("a.py") is None
        assert coord.append_note("note") is False
        assert coord.project_summary() == ""
        assert coord.other_chains() == []


def test_inactive_when_project_dir_missing(tmp_path):
    """Non-existent path → inactive; no blowup."""
    bogus = tmp_path / "does-not-exist"
    c = cc.ChainCoordinatorContext(bogus, "c1")
    assert c.active is False
    with c:
        pass  # must not raise


def test_inactive_when_project_dir_is_a_file(tmp_path):
    """A regular file is not a usable project dir → inactive."""
    f = tmp_path / "README.md"
    f.write_text("not a dir")
    c = cc.ChainCoordinatorContext(f, "c1")
    assert c.active is False


def test_active_when_project_dir_exists(tmp_path):
    c = cc.ChainCoordinatorContext(tmp_path, "c1", heartbeat_interval=0)
    assert c.active is True
    assert c.project_dir == tmp_path


def test_active_accepts_string_path(tmp_path):
    c = cc.ChainCoordinatorContext(str(tmp_path), "c1", heartbeat_interval=0)
    assert c.active is True
    assert c.project_dir == tmp_path


# --- Register / deregister lifecycle --------------------------------


def test_enter_registers_and_exit_deregisters(tmp_path):
    with cc.ChainCoordinatorContext(
        tmp_path, "c1", focus="backend", heartbeat_interval=0,
    ) as coord:
        # While inside: presence exists on disk.
        rec = pc.get_chain(tmp_path, "c1")
        assert rec is not None
        assert rec.focus == "backend"
        assert coord.chain_id == "c1"
    # After exit: deregister has cleared the presence.
    assert pc.get_chain(tmp_path, "c1") is None


def test_exit_tolerates_never_registered(tmp_path):
    """If register_chain raises on enter, exit must not raise either."""
    with mock.patch.object(pc, "register_chain", side_effect=OSError("disk full")):
        c = cc.ChainCoordinatorContext(tmp_path, "c1", heartbeat_interval=0)
        # Entering must not propagate.
        with c:
            # Confirm we logged the failure but stayed inactive-ish.
            # (active is True because project_dir is a real dir, but
            # _registered stays False.)
            assert c.active is True
        # And exiting without having registered must not call deregister
        # against a nonexistent presence — or if it does, it must swallow.
        assert pc.get_chain(tmp_path, "c1") is None


def test_exit_swallows_deregister_failure(tmp_path):
    """A deregister exception on exit must not propagate."""
    # Use heartbeat_interval=0 so no thread complications.
    c = cc.ChainCoordinatorContext(tmp_path, "c1", heartbeat_interval=0)
    c.__enter__()
    with mock.patch.object(pc, "deregister_chain", side_effect=OSError("broken")):
        c.__exit__(None, None, None)  # must not raise


def test_register_failure_skips_heartbeat_thread(tmp_path):
    """If register fails, the heartbeat thread must not start."""
    with mock.patch.object(pc, "register_chain", side_effect=OSError("boom")):
        c = cc.ChainCoordinatorContext(
            tmp_path, "c1", heartbeat_interval=0.01,
        )
        with c:
            assert c._thread is None  # no thread spun up


def test_enter_with_empty_chain_id_is_safe_noop(tmp_path):
    """Empty chain_id → skip register; exit is still clean."""
    c = cc.ChainCoordinatorContext(tmp_path, "", heartbeat_interval=0)
    with c as coord:
        assert coord.heartbeat() is False  # not registered
    # No presence was written.
    assert pc.coordination_path(tmp_path).exists() is False or \
        pc.get_chain(tmp_path, "") is None


def test_exception_inside_with_still_deregisters(tmp_path):
    """User code raising inside the block must not strand presence."""
    with pytest.raises(RuntimeError, match="boom"):
        with cc.ChainCoordinatorContext(
            tmp_path, "c1", heartbeat_interval=0,
        ):
            raise RuntimeError("boom")
    assert pc.get_chain(tmp_path, "c1") is None


# --- Heartbeat thread -----------------------------------------------


def test_heartbeat_thread_runs_on_interval(tmp_path):
    """Background thread calls pc.heartbeat at roughly `interval` cadence."""
    pc.register_chain(tmp_path, "c1", focus="")
    first = pc.get_chain(tmp_path, "c1").last_seen
    call_count = {"n": 0}

    real_heartbeat = pc.heartbeat

    def counting_heartbeat(project_dir, chain_id):
        call_count["n"] += 1
        return real_heartbeat(project_dir, chain_id)

    with mock.patch.object(pc, "heartbeat", side_effect=counting_heartbeat):
        # Use a very short interval so the test finishes quickly.
        with cc.ChainCoordinatorContext(
            tmp_path, "c1", heartbeat_interval=0.05,
        ):
            assert _wait_for(lambda: call_count["n"] >= 2, timeout=2.0)

    later = pc.get_chain(tmp_path, "c1")
    if later is not None:  # deregister already ran by this point
        assert later.last_seen >= first


def test_heartbeat_interval_zero_disables_thread(tmp_path):
    with cc.ChainCoordinatorContext(
        tmp_path, "c1", heartbeat_interval=0,
    ) as coord:
        assert coord._thread is None


def test_heartbeat_interval_negative_disables_thread(tmp_path):
    with cc.ChainCoordinatorContext(
        tmp_path, "c1", heartbeat_interval=-5,
    ) as coord:
        assert coord._thread is None


def test_heartbeat_thread_stops_on_exit(tmp_path):
    """After exit the heartbeat thread must have terminated."""
    c = cc.ChainCoordinatorContext(tmp_path, "c1", heartbeat_interval=0.02)
    c.__enter__()
    thread = c._thread
    assert thread is not None
    assert thread.is_alive()
    c.__exit__(None, None, None)
    # join(timeout=2.0) inside __exit__ gives the thread time to notice.
    assert not thread.is_alive()


def test_heartbeat_thread_swallows_exceptions(tmp_path):
    """A heartbeat call that raises must not crash the background thread."""
    pc.register_chain(tmp_path, "c1")
    # First call raises, subsequent calls succeed — prove the thread
    # keeps going after a transient failure.
    calls = {"n": 0}

    real = pc.heartbeat

    def flaky(project_dir, chain_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("transient")
        return real(project_dir, chain_id)

    with mock.patch.object(pc, "heartbeat", side_effect=flaky):
        with cc.ChainCoordinatorContext(
            tmp_path, "c1", heartbeat_interval=0.02,
        ):
            assert _wait_for(lambda: calls["n"] >= 3, timeout=2.0)


# --- Manual heartbeat -----------------------------------------------


def test_manual_heartbeat_returns_true_on_success(tmp_path):
    with cc.ChainCoordinatorContext(
        tmp_path, "c1", heartbeat_interval=0,
    ) as coord:
        assert coord.heartbeat() is True


def test_manual_heartbeat_swallows_exception(tmp_path):
    with cc.ChainCoordinatorContext(
        tmp_path, "c1", heartbeat_interval=0,
    ) as coord:
        with mock.patch.object(pc, "heartbeat", side_effect=OSError("x")):
            assert coord.heartbeat() is False


# --- Focus ----------------------------------------------------------


def test_set_focus_updates_underlying_state(tmp_path):
    with cc.ChainCoordinatorContext(
        tmp_path, "c1", focus="initial", heartbeat_interval=0,
    ) as coord:
        assert coord.focus == "initial"
        assert coord.set_focus("new focus") is True
        assert coord.focus == "new focus"
        rec = pc.get_chain(tmp_path, "c1")
        assert rec.focus == "new focus"


def test_set_focus_accepts_empty_string(tmp_path):
    with cc.ChainCoordinatorContext(
        tmp_path, "c1", focus="initial", heartbeat_interval=0,
    ) as coord:
        assert coord.set_focus("") is True


def test_set_focus_returns_false_if_not_registered(tmp_path):
    """Inactive or never-registered contexts return False without raising."""
    c = cc.ChainCoordinatorContext(None, "c1", heartbeat_interval=0)
    with c as coord:
        assert coord.set_focus("anything") is False


def test_set_focus_swallows_exception(tmp_path):
    with cc.ChainCoordinatorContext(
        tmp_path, "c1", heartbeat_interval=0,
    ) as coord:
        with mock.patch.object(pc, "set_focus", side_effect=OSError("x")):
            assert coord.set_focus("nope") is False


# --- File claims ----------------------------------------------------


def test_claim_files_success(tmp_path):
    with cc.ChainCoordinatorContext(
        tmp_path, "c1", heartbeat_interval=0,
    ) as coord:
        ok, conflicts = coord.claim_files(["auth.py", "routes.py"])
        assert ok is True
        assert conflicts == []
        rec = pc.get_chain(tmp_path, "c1")
        assert "auth.py" in rec.files_claimed
        assert "routes.py" in rec.files_claimed


def test_claim_files_conflict_with_other_chain(tmp_path):
    """Files held by a live other chain block the claim."""
    # Pre-seed: c_other owns auth.py.
    pc.register_chain(tmp_path, "c_other")
    ok, conflicts = pc.claim_files(tmp_path, "c_other", ["auth.py"])
    assert ok

    with cc.ChainCoordinatorContext(
        tmp_path, "c1", heartbeat_interval=0,
    ) as coord:
        ok, conflicts = coord.claim_files(["auth.py", "routes.py"])
        assert ok is False
        assert "auth.py" in conflicts
        # Nothing should have been claimed for c1 on a failed attempt.
        rec = pc.get_chain(tmp_path, "c1")
        assert rec.files_claimed == []


def test_claim_files_inactive_returns_empty(tmp_path):
    c = cc.ChainCoordinatorContext(None, "c1", heartbeat_interval=0)
    with c as coord:
        assert coord.claim_files(["x.py"]) == (False, [])


def test_claim_files_swallows_exception(tmp_path):
    with cc.ChainCoordinatorContext(
        tmp_path, "c1", heartbeat_interval=0,
    ) as coord:
        with mock.patch.object(pc, "claim_files", side_effect=OSError("x")):
            assert coord.claim_files(["a"]) == (False, [])


def test_release_files_specific(tmp_path):
    with cc.ChainCoordinatorContext(
        tmp_path, "c1", heartbeat_interval=0,
    ) as coord:
        coord.claim_files(["a.py", "b.py"])
        released = coord.release_files(["a.py"])
        assert released == 1
        rec = pc.get_chain(tmp_path, "c1")
        assert rec.files_claimed == ["b.py"]


def test_release_files_all_when_none(tmp_path):
    with cc.ChainCoordinatorContext(
        tmp_path, "c1", heartbeat_interval=0,
    ) as coord:
        coord.claim_files(["a.py", "b.py"])
        released = coord.release_files()
        assert released == 2
        rec = pc.get_chain(tmp_path, "c1")
        assert rec.files_claimed == []


def test_release_files_swallows_exception(tmp_path):
    with cc.ChainCoordinatorContext(
        tmp_path, "c1", heartbeat_interval=0,
    ) as coord:
        with mock.patch.object(pc, "release_files", side_effect=OSError("x")):
            assert coord.release_files() == 0


# --- file_owner -----------------------------------------------------


def test_file_owner_sees_our_own_claim(tmp_path):
    with cc.ChainCoordinatorContext(
        tmp_path, "c1", heartbeat_interval=0,
    ) as coord:
        coord.claim_files(["auth.py"])
        assert coord.file_owner("auth.py") == "c1"


def test_file_owner_returns_none_when_unclaimed(tmp_path):
    with cc.ChainCoordinatorContext(
        tmp_path, "c1", heartbeat_interval=0,
    ) as coord:
        assert coord.file_owner("auth.py") is None


def test_file_owner_returns_other_chains_id(tmp_path):
    pc.register_chain(tmp_path, "c_other")
    pc.claim_files(tmp_path, "c_other", ["auth.py"])
    with cc.ChainCoordinatorContext(
        tmp_path, "c1", heartbeat_interval=0,
    ) as coord:
        assert coord.file_owner("auth.py") == "c_other"


def test_file_owner_swallows_exception(tmp_path):
    with cc.ChainCoordinatorContext(
        tmp_path, "c1", heartbeat_interval=0,
    ) as coord:
        with mock.patch.object(pc, "file_owner", side_effect=OSError("x")):
            assert coord.file_owner("auth.py") is None


def test_file_owner_inactive_returns_none(tmp_path):
    c = cc.ChainCoordinatorContext(None, "c1")
    assert c.file_owner("x.py") is None


# --- append_note ----------------------------------------------------


def test_append_note_writes_activity(tmp_path):
    with cc.ChainCoordinatorContext(
        tmp_path, "c1", heartbeat_interval=0,
    ) as coord:
        assert coord.append_note("finished auth", ["auth.py"]) is True
        entries = pc.read_activity(tmp_path, limit=10)
        assert any(e.kind == "note" and e.summary == "finished auth" for e in entries)


def test_append_note_inactive_returns_false(tmp_path):
    c = cc.ChainCoordinatorContext(None, "c1")
    assert c.append_note("nope") is False


def test_append_note_swallows_exception(tmp_path):
    with cc.ChainCoordinatorContext(
        tmp_path, "c1", heartbeat_interval=0,
    ) as coord:
        with mock.patch.object(pc, "append_note", side_effect=OSError("x")):
            assert coord.append_note("nope") is False


# --- project_summary ------------------------------------------------


def test_project_summary_returns_string(tmp_path):
    with cc.ChainCoordinatorContext(
        tmp_path, "c1", focus="backend", heartbeat_interval=0,
    ) as coord:
        text = coord.project_summary()
        assert isinstance(text, str)
        assert "c1" in text
        assert "backend" in text


def test_project_summary_inactive_empty(tmp_path):
    c = cc.ChainCoordinatorContext(None, "c1")
    assert c.project_summary() == ""


def test_project_summary_swallows_exception(tmp_path):
    with cc.ChainCoordinatorContext(
        tmp_path, "c1", heartbeat_interval=0,
    ) as coord:
        with mock.patch.object(pc, "render_summary", side_effect=OSError("x")):
            assert coord.project_summary() == ""


# --- other_chains ---------------------------------------------------


def test_other_chains_excludes_self(tmp_path):
    pc.register_chain(tmp_path, "peer1", focus="A")
    pc.register_chain(tmp_path, "peer2", focus="B")
    with cc.ChainCoordinatorContext(
        tmp_path, "me", focus="self", heartbeat_interval=0,
    ) as coord:
        others = coord.other_chains()
        ids = {c.chain_id for c in others}
        assert ids == {"peer1", "peer2"}
        assert "me" not in ids


def test_other_chains_empty_when_alone(tmp_path):
    with cc.ChainCoordinatorContext(
        tmp_path, "only", heartbeat_interval=0,
    ) as coord:
        assert coord.other_chains() == []


def test_other_chains_inactive_empty(tmp_path):
    c = cc.ChainCoordinatorContext(None, "c1")
    assert c.other_chains() == []


def test_other_chains_swallows_exception(tmp_path):
    with cc.ChainCoordinatorContext(
        tmp_path, "c1", heartbeat_interval=0,
    ) as coord:
        with mock.patch.object(pc, "list_chains", side_effect=OSError("x")):
            assert coord.other_chains() == []


# --- Multi-chain integration ---------------------------------------


def test_two_concurrent_contexts_on_same_project(tmp_path):
    """Two chains can register simultaneously without interference."""
    c1 = cc.ChainCoordinatorContext(tmp_path, "a", focus="backend",
                                     heartbeat_interval=0)
    c2 = cc.ChainCoordinatorContext(tmp_path, "b", focus="tests",
                                     heartbeat_interval=0)
    with c1, c2:
        present = {c.chain_id for c in pc.list_chains(tmp_path)}
        assert present == {"a", "b"}
        assert c1.other_chains()[0].chain_id == "b"
        assert c2.other_chains()[0].chain_id == "a"
    # Both deregistered on exit.
    assert pc.list_chains(tmp_path) == []


def test_two_contexts_cannot_double_claim_same_file(tmp_path):
    """Second chain's claim on an already-held file returns conflict."""
    with cc.ChainCoordinatorContext(
        tmp_path, "a", heartbeat_interval=0,
    ) as ca, cc.ChainCoordinatorContext(
        tmp_path, "b", heartbeat_interval=0,
    ) as cb:
        ok, _ = ca.claim_files(["hot.py"])
        assert ok
        ok, conflicts = cb.claim_files(["hot.py", "cool.py"])
        assert ok is False
        assert "hot.py" in conflicts
