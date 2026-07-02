"""Tests for project_coordinator — presence + file claims + activity log.

All tests pin the entire state file under a tmp_path. Concurrency is
exercised via threads with a shared tmp_path; that hits the same fcntl
serialization the real multi-chain case relies on.
"""

import json
import sys
import threading
import time
from pathlib import Path

import pytest

_project_root = str(Path(__file__).parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import project_coordinator as pc
from project_coordinator import (
    ActivityEntry,
    ChainPresence,
    PRESENCE_TTL_DEFAULT,
)


# --- Path helpers ---

def test_coordination_dir_and_path(tmp_path):
    assert pc.coordination_dir(tmp_path) == tmp_path / ".dialectic"
    assert pc.coordination_path(tmp_path) == tmp_path / ".dialectic" / "coordination.json"


def test_register_creates_dialectic_dir_lazily(tmp_path):
    """The .dialectic dir doesn't need to pre-exist — register creates it."""
    assert not (tmp_path / ".dialectic").exists()
    pc.register_chain(tmp_path, "c1", focus="x")
    assert (tmp_path / ".dialectic").is_dir()
    assert pc.coordination_path(tmp_path).exists()


# --- register / heartbeat / deregister ---

def test_register_chain_writes_presence(tmp_path):
    rec = pc.register_chain(tmp_path, "c1", focus="backend api")
    assert isinstance(rec, ChainPresence)
    assert rec.chain_id == "c1"
    assert rec.focus == "backend api"
    assert rec.started_at > 0
    assert rec.last_seen >= rec.started_at
    assert rec.files_claimed == []

    # On disk
    data = json.loads(pc.coordination_path(tmp_path).read_text())
    assert "c1" in data["chains"]
    assert data["chains"]["c1"]["focus"] == "backend api"


def test_register_empty_chain_id_rejected(tmp_path):
    with pytest.raises(ValueError):
        pc.register_chain(tmp_path, "", focus="x")


def test_re_register_preserves_started_at_and_claims(tmp_path):
    """Re-registering the same id refreshes presence without dropping claims."""
    first = pc.register_chain(tmp_path, "c1", focus="initial")
    pc.claim_files(tmp_path, "c1", ["a.py"])
    time.sleep(0.01)
    second = pc.register_chain(tmp_path, "c1", focus="updated")
    assert second.started_at == first.started_at
    assert second.focus == "updated"
    # Existing claims survive the re-register.
    fresh = pc.get_chain(tmp_path, "c1")
    assert fresh.files_claimed == ["a.py"]


def test_re_register_with_empty_focus_does_not_clear(tmp_path):
    """Passing focus='' on re-register keeps the previous focus."""
    pc.register_chain(tmp_path, "c1", focus="keep me")
    pc.register_chain(tmp_path, "c1", focus="")
    assert pc.get_chain(tmp_path, "c1").focus == "keep me"


def test_heartbeat_bumps_last_seen(tmp_path):
    pc.register_chain(tmp_path, "c1")
    before = pc.get_chain(tmp_path, "c1").last_seen
    time.sleep(0.02)
    assert pc.heartbeat(tmp_path, "c1") is True
    after = pc.get_chain(tmp_path, "c1").last_seen
    assert after > before


def test_heartbeat_unknown_chain_returns_false(tmp_path):
    pc.register_chain(tmp_path, "c1")
    assert pc.heartbeat(tmp_path, "nope") is False


def test_set_focus_records_activity(tmp_path):
    pc.register_chain(tmp_path, "c1", focus="old")
    assert pc.set_focus(tmp_path, "c1", "new") is True
    assert pc.get_chain(tmp_path, "c1").focus == "new"
    activity = pc.read_activity(tmp_path)
    kinds = [e.kind for e in activity]
    assert "focus" in kinds
    focus_entry = [e for e in activity if e.kind == "focus"][-1]
    assert focus_entry.summary == "new"


def test_set_focus_unknown_chain_returns_false(tmp_path):
    assert pc.set_focus(tmp_path, "nope", "anything") is False


def test_deregister_removes_presence(tmp_path):
    pc.register_chain(tmp_path, "c1")
    pc.register_chain(tmp_path, "c2")
    assert pc.deregister_chain(tmp_path, "c1") is True
    chains = pc.list_chains(tmp_path, ttl=0)
    assert {c.chain_id for c in chains} == {"c2"}
    activity = [e for e in pc.read_activity(tmp_path) if e.kind == "deregister"]
    assert any(e.chain_id == "c1" for e in activity)


def test_deregister_unknown_returns_false(tmp_path):
    assert pc.deregister_chain(tmp_path, "ghost") is False


# --- list / get ---

def test_list_chains_empty(tmp_path):
    assert pc.list_chains(tmp_path) == []


def test_list_chains_sorted_by_started_at(tmp_path):
    pc.register_chain(tmp_path, "c1")
    time.sleep(0.01)
    pc.register_chain(tmp_path, "c2")
    time.sleep(0.01)
    pc.register_chain(tmp_path, "c3")
    chains = pc.list_chains(tmp_path)
    assert [c.chain_id for c in chains] == ["c1", "c2", "c3"]


def test_list_chains_filters_by_ttl(tmp_path):
    """Chains older than ttl are filtered out (read-only — file unchanged)."""
    pc.register_chain(tmp_path, "fresh")
    pc.register_chain(tmp_path, "stale")
    # Manually age "stale" to be far in the past.
    data = json.loads(pc.coordination_path(tmp_path).read_text())
    data["chains"]["stale"]["last_seen"] = time.time() - 10000
    pc.coordination_path(tmp_path).write_text(json.dumps(data))

    visible = pc.list_chains(tmp_path, ttl=60)
    assert {c.chain_id for c in visible} == {"fresh"}
    # File should NOT have been pruned by a read.
    raw = json.loads(pc.coordination_path(tmp_path).read_text())
    assert "stale" in raw["chains"]


def test_list_chains_ttl_zero_returns_all(tmp_path):
    """ttl=0 disables the filter."""
    pc.register_chain(tmp_path, "fresh")
    pc.register_chain(tmp_path, "stale")
    data = json.loads(pc.coordination_path(tmp_path).read_text())
    data["chains"]["stale"]["last_seen"] = time.time() - 10000
    pc.coordination_path(tmp_path).write_text(json.dumps(data))

    visible = pc.list_chains(tmp_path, ttl=0)
    assert {c.chain_id for c in visible} == {"fresh", "stale"}


def test_get_chain_present_and_missing(tmp_path):
    pc.register_chain(tmp_path, "c1", focus="x")
    assert pc.get_chain(tmp_path, "c1").focus == "x"
    assert pc.get_chain(tmp_path, "missing") is None


# --- claim_files ---

def test_claim_files_happy_path(tmp_path):
    pc.register_chain(tmp_path, "c1")
    ok, conflicts = pc.claim_files(tmp_path, "c1", ["a.py", "b.py"])
    assert ok is True and conflicts == []
    assert pc.get_chain(tmp_path, "c1").files_claimed == ["a.py", "b.py"]


def test_claim_files_records_activity(tmp_path):
    pc.register_chain(tmp_path, "c1")
    pc.claim_files(tmp_path, "c1", ["x.py"])
    activity = pc.read_activity(tmp_path)
    claim_entries = [e for e in activity if e.kind == "claim"]
    assert len(claim_entries) == 1
    assert claim_entries[0].files == ["x.py"]


def test_claim_files_idempotent_for_owner(tmp_path):
    """Re-claiming files you already own returns success without duplicating."""
    pc.register_chain(tmp_path, "c1")
    pc.claim_files(tmp_path, "c1", ["a.py"])
    ok, conflicts = pc.claim_files(tmp_path, "c1", ["a.py", "b.py"])
    assert ok is True and conflicts == []
    assert pc.get_chain(tmp_path, "c1").files_claimed == ["a.py", "b.py"]


def test_claim_files_conflict_with_live_other(tmp_path):
    pc.register_chain(tmp_path, "c1")
    pc.register_chain(tmp_path, "c2")
    pc.claim_files(tmp_path, "c1", ["shared.py", "c1-only.py"])

    ok, conflicts = pc.claim_files(tmp_path, "c2", ["shared.py", "c2-only.py"])
    assert ok is False
    assert conflicts == ["shared.py"]
    # On conflict, NOTHING was added — c2 should not own c2-only.py either.
    assert pc.get_chain(tmp_path, "c2").files_claimed == []


def test_claim_files_silently_transfers_from_stale_owner(tmp_path):
    """Stale chains' claims don't gridlock the project."""
    pc.register_chain(tmp_path, "stale")
    pc.register_chain(tmp_path, "active")
    pc.claim_files(tmp_path, "stale", ["foo.py"])

    # Age "stale" past the TTL.
    data = json.loads(pc.coordination_path(tmp_path).read_text())
    data["chains"]["stale"]["last_seen"] = time.time() - 10000
    pc.coordination_path(tmp_path).write_text(json.dumps(data))

    ok, conflicts = pc.claim_files(tmp_path, "active", ["foo.py"], ttl=60)
    assert ok is True and conflicts == []
    assert pc.get_chain(tmp_path, "active").files_claimed == ["foo.py"]
    # Stale chain no longer holds the file.
    assert "foo.py" not in pc.get_chain(tmp_path, "stale").files_claimed


def test_claim_files_unregistered_chain_raises(tmp_path):
    with pytest.raises(ValueError):
        pc.claim_files(tmp_path, "ghost", ["x.py"])


def test_claim_files_accepts_path_objects_and_dedupes(tmp_path):
    pc.register_chain(tmp_path, "c1")
    ok, _ = pc.claim_files(
        tmp_path, "c1",
        [Path("a.py"), "a.py", Path("dir/b.py")],
    )
    assert ok is True
    assert pc.get_chain(tmp_path, "c1").files_claimed == ["a.py", "dir/b.py"]


# --- release_files ---

def test_release_files_specific_subset(tmp_path):
    pc.register_chain(tmp_path, "c1")
    pc.claim_files(tmp_path, "c1", ["a.py", "b.py", "c.py"])
    n = pc.release_files(tmp_path, "c1", ["a.py", "c.py"])
    assert n == 2
    assert pc.get_chain(tmp_path, "c1").files_claimed == ["b.py"]


def test_release_files_all_when_files_none(tmp_path):
    pc.register_chain(tmp_path, "c1")
    pc.claim_files(tmp_path, "c1", ["a.py", "b.py"])
    n = pc.release_files(tmp_path, "c1")
    assert n == 2
    assert pc.get_chain(tmp_path, "c1").files_claimed == []


def test_release_files_idempotent_on_unowned(tmp_path):
    """Releasing files you don't hold is a no-op (returns 0)."""
    pc.register_chain(tmp_path, "c1")
    pc.claim_files(tmp_path, "c1", ["a.py"])
    n = pc.release_files(tmp_path, "c1", ["never-held.py"])
    assert n == 0
    assert pc.get_chain(tmp_path, "c1").files_claimed == ["a.py"]


def test_release_files_unregistered_chain_raises(tmp_path):
    with pytest.raises(ValueError):
        pc.release_files(tmp_path, "ghost", ["x.py"])


def test_release_files_records_activity(tmp_path):
    pc.register_chain(tmp_path, "c1")
    pc.claim_files(tmp_path, "c1", ["a.py", "b.py"])
    pc.release_files(tmp_path, "c1", ["a.py"])
    release_entries = [e for e in pc.read_activity(tmp_path) if e.kind == "release"]
    assert len(release_entries) == 1
    assert release_entries[0].files == ["a.py"]


# --- file_owner ---

def test_file_owner_returns_chain_id(tmp_path):
    pc.register_chain(tmp_path, "c1")
    pc.claim_files(tmp_path, "c1", ["x.py"])
    assert pc.file_owner(tmp_path, "x.py") == "c1"


def test_file_owner_none_when_unclaimed(tmp_path):
    pc.register_chain(tmp_path, "c1")
    assert pc.file_owner(tmp_path, "nobody.py") is None


def test_file_owner_ignores_stale_owner_by_default(tmp_path):
    pc.register_chain(tmp_path, "stale")
    pc.claim_files(tmp_path, "stale", ["x.py"])
    data = json.loads(pc.coordination_path(tmp_path).read_text())
    data["chains"]["stale"]["last_seen"] = time.time() - 10000
    pc.coordination_path(tmp_path).write_text(json.dumps(data))

    assert pc.file_owner(tmp_path, "x.py", ttl=60) is None
    # ttl=0 disables staleness filter and returns the stale owner.
    assert pc.file_owner(tmp_path, "x.py", ttl=0) == "stale"


# --- release_stale ---

def test_release_stale_drops_old_chains(tmp_path):
    pc.register_chain(tmp_path, "fresh")
    pc.register_chain(tmp_path, "stale1")
    pc.register_chain(tmp_path, "stale2")
    data = json.loads(pc.coordination_path(tmp_path).read_text())
    data["chains"]["stale1"]["last_seen"] = time.time() - 10000
    data["chains"]["stale2"]["last_seen"] = time.time() - 10000
    pc.coordination_path(tmp_path).write_text(json.dumps(data))

    removed = pc.release_stale(tmp_path, ttl=60)
    assert sorted(removed) == ["stale1", "stale2"]
    assert {c.chain_id for c in pc.list_chains(tmp_path, ttl=0)} == {"fresh"}
    # Activity log records the eviction.
    notes = [e for e in pc.read_activity(tmp_path)
             if e.kind == "deregister" and e.summary == "released-stale"]
    assert {e.chain_id for e in notes} == {"stale1", "stale2"}


# --- activity log ---

def test_append_note_and_read(tmp_path):
    pc.append_note(tmp_path, "c1", "shipped recap polish", files=["chain.py"])
    entries = pc.read_activity(tmp_path)
    assert len(entries) == 1
    assert entries[0].kind == "note"
    assert entries[0].files == ["chain.py"]
    assert entries[0].summary == "shipped recap polish"


def test_append_note_works_without_registration(tmp_path):
    """Notes from deregistered chains are still useful audit trail."""
    pc.append_note(tmp_path, "departed", "saying goodbye")
    entries = pc.read_activity(tmp_path)
    assert entries[0].chain_id == "departed"


def test_read_activity_since_filter(tmp_path):
    pc.append_note(tmp_path, "c1", "first")
    cutoff = time.time()
    time.sleep(0.02)
    pc.append_note(tmp_path, "c1", "second")
    later = pc.read_activity(tmp_path, since=cutoff)
    assert [e.summary for e in later] == ["second"]


def test_read_activity_limit(tmp_path):
    for i in range(10):
        pc.append_note(tmp_path, "c1", f"note{i}")
    last3 = pc.read_activity(tmp_path, limit=3)
    assert [e.summary for e in last3] == ["note7", "note8", "note9"]


def test_activity_log_bounded(tmp_path):
    """Log doesn't grow without bound — oldest entries are dropped."""
    cap = pc.ACTIVITY_LOG_MAX
    for i in range(cap + 25):
        pc.append_note(tmp_path, "c1", f"note{i}")
    data = json.loads(pc.coordination_path(tmp_path).read_text())
    assert len(data["activity"]) == cap
    # The earliest 25 should have rotated out.
    assert data["activity"][0]["summary"] == "note25"
    assert data["activity"][-1]["summary"] == f"note{cap + 24}"


# --- claim_rate ---

def _backdate_claim(project_dir, file_name_substring, delta_seconds):
    """Backdate the 'claim' activity entry whose files list contains a match.

    Lets us plant entries at any apparent age without sleeping. Only the
    timestamp is edited; everything else the real API wrote stays intact,
    so claim_rate's cutoff logic is exercised against true activity rows.
    """
    path = pc.coordination_path(project_dir)
    data = json.loads(path.read_text())
    for e in data["activity"]:
        if e.get("kind") != "claim":
            continue
        if any(file_name_substring in f for f in e.get("files", [])):
            e["timestamp"] = time.time() - delta_seconds
            break
    path.write_text(json.dumps(data))


def test_claim_rate_empty_project_returns_empty(tmp_path):
    assert pc.claim_rate(tmp_path) == {}


def test_claim_rate_counts_claims_per_chain(tmp_path):
    pc.register_chain(tmp_path, "c1")
    pc.register_chain(tmp_path, "c2")
    pc.claim_files(tmp_path, "c1", ["a.py"])
    pc.claim_files(tmp_path, "c1", ["b.py"])
    pc.claim_files(tmp_path, "c2", ["d.py"])
    rates = pc.claim_rate(tmp_path)
    assert rates == {"c1": 2, "c2": 1}


def test_claim_rate_ignores_non_claim_activity(tmp_path):
    """register / deregister / focus / release / note entries are not claims."""
    pc.register_chain(tmp_path, "c1", focus="x")
    pc.set_focus(tmp_path, "c1", "y")
    pc.claim_files(tmp_path, "c1", ["a.py"])
    pc.release_files(tmp_path, "c1", ["a.py"])
    pc.append_note(tmp_path, "c1", "freeform")
    pc.deregister_chain(tmp_path, "c1")
    assert pc.claim_rate(tmp_path) == {"c1": 1}


def test_claim_rate_ignores_entries_outside_window(tmp_path):
    pc.register_chain(tmp_path, "c1")
    pc.claim_files(tmp_path, "c1", ["stale.py"])
    pc.claim_files(tmp_path, "c1", ["fresh.py"])
    # Push the first claim outside a 60s window; keep the second inside.
    _backdate_claim(tmp_path, "stale", delta_seconds=120)
    assert pc.claim_rate(tmp_path, window_seconds=60) == {"c1": 1}


def test_claim_rate_zero_window_returns_empty(tmp_path):
    """window_seconds <= 0 disables the metric entirely."""
    pc.register_chain(tmp_path, "c1")
    pc.claim_files(tmp_path, "c1", ["a.py"])
    assert pc.claim_rate(tmp_path, window_seconds=0) == {}
    assert pc.claim_rate(tmp_path, window_seconds=-5) == {}


def test_claim_rate_omits_chains_with_no_claims_in_window(tmp_path):
    """A chain registered but never claiming should not appear in the result."""
    pc.register_chain(tmp_path, "c1")  # never claims
    pc.register_chain(tmp_path, "c2")
    pc.claim_files(tmp_path, "c2", ["a.py"])
    rates = pc.claim_rate(tmp_path)
    assert "c1" not in rates
    assert rates["c2"] == 1


def test_claim_rate_counts_each_claim_call_once(tmp_path):
    """Multi-file claims emit one activity entry, so the count is 1 — the
    metric measures protocol invocation frequency, not file count."""
    pc.register_chain(tmp_path, "c1")
    pc.claim_files(tmp_path, "c1", ["a.py", "b.py", "c.py"])
    assert pc.claim_rate(tmp_path) == {"c1": 1}


# --- render_summary ---

def test_render_summary_empty(tmp_path):
    out = pc.render_summary(tmp_path)
    assert "No active chains" in out


def test_render_summary_lists_chains_focuses_and_claims(tmp_path):
    pc.register_chain(tmp_path, "c1", focus="backend api")
    pc.register_chain(tmp_path, "c2", focus="tests for auth")
    pc.claim_files(tmp_path, "c1", ["src/api.py", "src/db.py"])
    pc.append_note(tmp_path, "c1", "wired router")

    out = pc.render_summary(tmp_path)
    assert "c1" in out
    assert "c2" in out
    assert "backend api" in out
    assert "tests for auth" in out
    assert "src/api.py" in out
    assert "Recent activity" in out
    assert "wired router" in out


def test_render_summary_handles_no_focus_gracefully(tmp_path):
    pc.register_chain(tmp_path, "c1")
    out = pc.render_summary(tmp_path)
    assert "(no focus set)" in out


# --- corruption / robustness ---

def test_corrupted_json_recovered_to_empty(tmp_path):
    """A truncated/garbled file shouldn't crash the next reader."""
    coord_dir = tmp_path / ".dialectic"
    coord_dir.mkdir()
    (coord_dir / "coordination.json").write_text("{not valid json{")
    # Read path
    assert pc.list_chains(tmp_path) == []
    assert pc.read_activity(tmp_path) == []
    # Write path: registering should overwrite the corruption with valid JSON.
    pc.register_chain(tmp_path, "c1")
    assert pc.get_chain(tmp_path, "c1").chain_id == "c1"


def test_missing_file_treated_as_empty(tmp_path):
    """Listing on a project that's never been touched works without errors."""
    assert pc.list_chains(tmp_path) == []
    assert pc.file_owner(tmp_path, "anything.py") is None
    assert "No active chains" in pc.render_summary(tmp_path)


# --- _normalize_files ---

def test_normalize_files_dedupes_preserves_order_accepts_path(tmp_path):
    out = pc._normalize_files([Path("a"), "b", "a", Path("b"), "c"])
    assert out == ["a", "b", "c"]


# --- concurrency ---

def test_concurrent_registers_and_claims_are_serialized(tmp_path):
    """Two threads claim disjoint files simultaneously → both succeed, no torn writes."""
    pc.register_chain(tmp_path, "c1")
    pc.register_chain(tmp_path, "c2")

    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def claim(chain_id, files):
        try:
            barrier.wait(timeout=2)
            for f in files:
                ok, conflicts = pc.claim_files(tmp_path, chain_id, [f])
                assert ok is True, f"{chain_id} blocked on {f}: {conflicts}"
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=claim, args=("c1", [f"c1-{i}.py" for i in range(8)]))
    t2 = threading.Thread(target=claim, args=("c2", [f"c2-{i}.py" for i in range(8)]))
    t1.start(); t2.start()
    t1.join(); t2.join()
    assert not errors

    c1 = pc.get_chain(tmp_path, "c1")
    c2 = pc.get_chain(tmp_path, "c2")
    assert len(c1.files_claimed) == 8
    assert len(c2.files_claimed) == 8
    # No file was lost due to a torn read-modify-write.
    assert set(c1.files_claimed).isdisjoint(c2.files_claimed)


def test_concurrent_claims_for_same_file_have_one_winner(tmp_path):
    """Two chains race for the same file. Exactly one wins; the other gets a conflict."""
    pc.register_chain(tmp_path, "c1")
    pc.register_chain(tmp_path, "c2")

    results: list[tuple[str, bool]] = []
    barrier = threading.Barrier(2)

    def claim(chain_id):
        barrier.wait(timeout=2)
        ok, _ = pc.claim_files(tmp_path, chain_id, ["contested.py"])
        results.append((chain_id, ok))

    t1 = threading.Thread(target=claim, args=("c1",))
    t2 = threading.Thread(target=claim, args=("c2",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    successes = [cid for cid, ok in results if ok]
    failures = [cid for cid, ok in results if not ok]
    assert len(successes) == 1, f"expected 1 winner, got {successes}"
    assert len(failures) == 1
    winner = successes[0]
    assert pc.file_owner(tmp_path, "contested.py") == winner


# --- CLI ---

def _run_cli(argv: list[str]) -> tuple[int, str]:
    """Invoke _cli_main, capture stdout for assertion."""
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = pc._cli_main(argv)
    return rc, buf.getvalue()


def test_cli_summary_empty(tmp_path):
    rc, out = _run_cli(["--project", str(tmp_path), "--summary"])
    assert rc == 0
    assert "No active chains" in out


def test_cli_summary_populated(tmp_path):
    pc.register_chain(tmp_path, "c1", focus="backend")
    pc.claim_files(tmp_path, "c1", ["api.py"])
    rc, out = _run_cli(["--project", str(tmp_path), "--summary"])
    assert rc == 0
    assert "c1" in out and "backend" in out and "api.py" in out


def test_cli_chains_lists_one_per_row(tmp_path):
    pc.register_chain(tmp_path, "a", focus="x")
    pc.register_chain(tmp_path, "b", focus="y")
    rc, out = _run_cli(["--project", str(tmp_path), "--chains"])
    assert rc == 0
    rows = [r for r in out.splitlines() if r.strip()]
    assert len(rows) == 2
    assert any(r.startswith("a\t") for r in rows)
    assert any(r.startswith("b\t") for r in rows)


def test_cli_chains_empty(tmp_path):
    rc, out = _run_cli(["--project", str(tmp_path), "--chains"])
    assert rc == 0
    assert "no active chains" in out.lower()


def test_cli_claims_lists_each_file(tmp_path):
    pc.register_chain(tmp_path, "c1")
    pc.claim_files(tmp_path, "c1", ["a.py", "b.py"])
    rc, out = _run_cli(["--project", str(tmp_path), "--claims"])
    assert rc == 0
    assert "a.py\tc1" in out and "b.py\tc1" in out


def test_cli_claims_empty(tmp_path):
    rc, out = _run_cli(["--project", str(tmp_path), "--claims"])
    assert rc == 0
    assert "no claims" in out.lower()


def test_cli_activity_prints_entries(tmp_path):
    pc.append_note(tmp_path, "c1", "did a thing")
    rc, out = _run_cli(["--project", str(tmp_path), "--activity"])
    assert rc == 0
    assert "c1" in out and "did a thing" in out


def test_cli_activity_empty(tmp_path):
    rc, out = _run_cli(["--project", str(tmp_path), "--activity"])
    assert rc == 0
    assert "no activity" in out.lower()


def test_cli_claim_rate_empty(tmp_path):
    rc, out = _run_cli(["--project", str(tmp_path), "--claim-rate"])
    assert rc == 0
    assert "no claim activity" in out.lower()


def test_cli_claim_rate_reports_per_chain(tmp_path):
    pc.register_chain(tmp_path, "c1")
    pc.register_chain(tmp_path, "c2")
    pc.claim_files(tmp_path, "c1", ["a.py"])
    pc.claim_files(tmp_path, "c1", ["b.py"])
    pc.claim_files(tmp_path, "c2", ["d.py"])

    rc, out = _run_cli(["--project", str(tmp_path), "--claim-rate"])
    assert rc == 0
    # Sorted by chain id; each row is "chain\tcount\tper-hour"
    rows = [r for r in out.splitlines() if r.strip()]
    assert len(rows) == 2
    assert rows[0].startswith("c1\t2\t")
    assert rows[0].endswith("/hr")
    assert rows[1].startswith("c2\t1\t")


def test_cli_claim_rate_honors_window(tmp_path):
    """--window scales the per-hour rate and filters entries outside the window."""
    pc.register_chain(tmp_path, "c1")
    pc.claim_files(tmp_path, "c1", ["stale.py"])
    pc.claim_files(tmp_path, "c1", ["fresh.py"])
    _backdate_claim(tmp_path, "stale", delta_seconds=200)

    # 60s window: only one claim should count.
    rc, out = _run_cli([
        "--project", str(tmp_path), "--claim-rate", "--window", "60",
    ])
    assert rc == 0
    assert "c1\t1\t" in out

    # 1000s window: both claims count.
    rc, out = _run_cli([
        "--project", str(tmp_path), "--claim-rate", "--window", "1000",
    ])
    assert rc == 0
    assert "c1\t2\t" in out


def test_cli_release_stale_prunes_and_reports(tmp_path):
    pc.register_chain(tmp_path, "stale")
    data = json.loads(pc.coordination_path(tmp_path).read_text())
    data["chains"]["stale"]["last_seen"] = time.time() - 10000
    pc.coordination_path(tmp_path).write_text(json.dumps(data))

    rc, out = _run_cli(["--project", str(tmp_path), "--release-stale", "--ttl", "60"])
    assert rc == 0
    assert "stale" in out
    assert pc.get_chain(tmp_path, "stale") is None


def test_cli_release_stale_nothing_to_do(tmp_path):
    pc.register_chain(tmp_path, "fresh")
    rc, out = _run_cli(["--project", str(tmp_path), "--release-stale"])
    assert rc == 0
    assert "Nothing stale" in out


def test_cli_missing_project_exits_one(tmp_path):
    missing = tmp_path / "does-not-exist"
    rc, out = _run_cli(["--project", str(missing), "--summary"])
    assert rc == 1
    assert "not found" in out.lower()


def test_cli_requires_a_command(tmp_path):
    """No mutually-exclusive flag → argparse exits 2 (SystemExit)."""
    with pytest.raises(SystemExit):
        _run_cli(["--project", str(tmp_path)])


# --- CLI: --claim / --release mutators ---

def test_cli_claim_registered_chain_succeeds(tmp_path):
    pc.register_chain(tmp_path, "c1", focus="x")
    rc, out = _run_cli([
        "--project", str(tmp_path), "--claim", "a.py", "b.py",
        "--chain", "c1",
    ])
    assert rc == 0
    assert "Claimed 2 file(s)" in out
    assert "c1" in out
    # State actually mutated on disk.
    assert pc.get_chain(tmp_path, "c1").files_claimed == ["a.py", "b.py"]


def test_cli_claim_unregistered_chain_exits_one(tmp_path):
    """claim_files raises ValueError for an unregistered chain; CLI → rc=1."""
    rc, out = _run_cli([
        "--project", str(tmp_path), "--claim", "a.py", "--chain", "ghost",
    ])
    assert rc == 1
    assert "not registered" in out.lower()


def test_cli_claim_without_chain_flag_exits_one(tmp_path):
    """--chain is required with --claim."""
    pc.register_chain(tmp_path, "c1")
    rc, out = _run_cli([
        "--project", str(tmp_path), "--claim", "a.py",
    ])
    assert rc == 1
    assert "--chain" in out.lower()


def test_cli_claim_conflict_exits_two(tmp_path):
    """A file held by a live other chain → exit 2, conflicts in stdout."""
    pc.register_chain(tmp_path, "c1")
    pc.register_chain(tmp_path, "c2")
    pc.claim_files(tmp_path, "c1", ["shared.py"])

    rc, out = _run_cli([
        "--project", str(tmp_path), "--claim", "shared.py", "--chain", "c2",
    ])
    assert rc == 2
    assert "shared.py" in out
    # c2 must not have gained the claim.
    assert pc.get_chain(tmp_path, "c2").files_claimed == []


def test_cli_release_specific_files(tmp_path):
    pc.register_chain(tmp_path, "c1")
    pc.claim_files(tmp_path, "c1", ["a.py", "b.py"])
    rc, out = _run_cli([
        "--project", str(tmp_path), "--release", "a.py", "--chain", "c1",
    ])
    assert rc == 0
    assert "Released 1" in out
    assert pc.get_chain(tmp_path, "c1").files_claimed == ["b.py"]


def test_cli_release_all_when_no_files_given(tmp_path):
    """--release with no file args releases every file the chain holds."""
    pc.register_chain(tmp_path, "c1")
    pc.claim_files(tmp_path, "c1", ["a.py", "b.py", "c.py"])
    rc, out = _run_cli([
        "--project", str(tmp_path), "--release", "--chain", "c1",
    ])
    assert rc == 0
    assert "Released 3" in out
    assert pc.get_chain(tmp_path, "c1").files_claimed == []


def test_cli_release_unregistered_chain_exits_one(tmp_path):
    rc, out = _run_cli([
        "--project", str(tmp_path), "--release", "a.py", "--chain", "ghost",
    ])
    assert rc == 1
    assert "not registered" in out.lower()


def test_cli_release_without_chain_flag_exits_one(tmp_path):
    pc.register_chain(tmp_path, "c1")
    pc.claim_files(tmp_path, "c1", ["a.py"])
    rc, out = _run_cli([
        "--project", str(tmp_path), "--release", "a.py",
    ])
    assert rc == 1
    assert "--chain" in out.lower()


def test_cli_claim_and_release_are_mutually_exclusive_with_summary(tmp_path):
    """The mutators live inside the same mutually-exclusive group as the
    read-only flags — passing both should error out via argparse."""
    pc.register_chain(tmp_path, "c1")
    with pytest.raises(SystemExit):
        _run_cli([
            "--project", str(tmp_path), "--summary",
            "--claim", "a.py", "--chain", "c1",
        ])
