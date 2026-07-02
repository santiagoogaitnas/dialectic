"""Tests for ui/project_view.py — grouped, blended project-first views.

project_view reads from two sources (registry.py + project_coordinator.py)
and merges them. The tests use tmp_path for both so a real filesystem is
exercised end-to-end; the cross-reference logic is the interesting part
that unit-style mocks would under-cover.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO = str(Path(__file__).parent.parent)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import project_coordinator as pc
import registry as reg
from registry import ChainConfig
from ui import project_view as pv


@pytest.fixture
def reg_patch(tmp_path):
    """Redirect registry state into the test tmp_path."""
    reg_file = tmp_path / ".registry.json"
    with patch.object(reg, "REGISTRY_FILE", reg_file), \
         patch.object(reg, "WORKSPACE", tmp_path):
        yield tmp_path


def _register(chain_id: str, project: str | None, seed: str = "x") -> None:
    """Shorthand for registering a chain against the patched registry."""
    cfg = ChainConfig(
        chain_id=chain_id, session=f"s-{chain_id}", seed=seed, project=project
    )
    reg.register_chain(cfg, os.getpid())


# ---------- list_projects_with_chains ----------


def test_list_projects_with_chains_empty(reg_patch):
    assert pv.list_projects_with_chains() == []


def test_list_projects_with_chains_groups_by_project(reg_patch, tmp_path):
    projA = tmp_path / "projA"
    projB = tmp_path / "projB"
    projA.mkdir()
    projB.mkdir()
    _register("cA1", str(projA))
    _register("cA2", str(projA))
    _register("cB1", str(projB))
    result = pv.list_projects_with_chains()
    keys = {entry["project"] for entry in result}
    assert keys == {str(projA), str(projB)}
    by_key = {entry["project"]: entry for entry in result}
    assert by_key[str(projA)]["chain_count"] == 2
    assert by_key[str(projB)]["chain_count"] == 1


def test_list_projects_with_chains_no_project_bucket(reg_patch):
    """A chain registered without a project surfaces in (no project)."""
    _register("orphan", None)
    result = pv.list_projects_with_chains()
    assert len(result) == 1
    assert result[0]["project"] == pv.NO_PROJECT_KEY
    assert result[0]["chain_count"] == 1


def test_list_projects_merges_focus_from_coordination(reg_patch, tmp_path):
    """A chain registered on a project + registered with coordinator surfaces focus."""
    project = tmp_path / "p"
    project.mkdir()
    _register("c1", str(project))
    pc.register_chain(project, "c1", focus="backend api")

    result = pv.list_projects_with_chains()
    entry = next(e for e in result if e["project"] == str(project))
    assert entry["chain_count"] == 1
    assert "backend api" in entry["focus_areas"]
    (chain,) = entry["chains"]
    assert chain["chain_id"] == "c1"
    assert chain["focus"] == "backend api"
    assert chain["has_registry"] is True
    assert chain["has_presence"] is True


def test_list_projects_preserves_presence_only_entries(reg_patch, tmp_path):
    """Presence with no matching registry row still appears, flagged."""
    project = tmp_path / "p"
    project.mkdir()
    _register("alive", str(project))
    pc.register_chain(project, "alive", focus="foo")
    pc.register_chain(project, "ghost", focus="stale")

    entry = next(
        e for e in pv.list_projects_with_chains() if e["project"] == str(project)
    )
    ids = {c["chain_id"] for c in entry["chains"]}
    assert ids == {"alive", "ghost"}
    ghost = next(c for c in entry["chains"] if c["chain_id"] == "ghost")
    assert ghost["has_registry"] is False
    assert ghost["has_presence"] is True


def test_list_projects_missing_coordination_file_is_fine(reg_patch, tmp_path):
    """No .dialectic/coordination.json yet — chain still listed, no focus."""
    project = tmp_path / "p"
    project.mkdir()
    _register("c1", str(project))
    entry = next(
        e for e in pv.list_projects_with_chains() if e["project"] == str(project)
    )
    assert entry["chain_count"] == 1
    (chain,) = entry["chains"]
    assert chain["focus"] == ""
    assert chain["files_claimed"] == []


def test_list_projects_sorts_by_active_then_name(reg_patch, tmp_path):
    """Projects with more active chains come first; ties break alphabetically."""
    projA = tmp_path / "a-proj"
    projB = tmp_path / "b-proj"
    projC = tmp_path / "c-proj"
    for p in (projA, projB, projC):
        p.mkdir()

    # A: 1 active, B: 2 active, C: 1 active
    _register("a1", str(projA))
    _register("b1", str(projB))
    _register("b2", str(projB))
    _register("c1", str(projC))

    result = pv.list_projects_with_chains()
    order = [e["project"] for e in result]
    # B (2 active) first; A and C tie on 1 active — alpha tiebreaker
    assert order[0] == str(projB)
    assert order[1:] == [str(projA), str(projC)]


def test_list_projects_ttl_filters_stale_presence(reg_patch, tmp_path):
    """Presence older than the ttl isn't merged into the chain's focus."""
    project = tmp_path / "p"
    project.mkdir()
    _register("c1", str(project))
    pc.register_chain(project, "c1", focus="old-focus")

    # Force the presence timestamp to long ago by rewriting the file.
    import json as _json
    coord = pc.coordination_path(project)
    data = _json.loads(coord.read_text())
    data["chains"]["c1"]["last_seen"] = 0.0
    coord.write_text(_json.dumps(data))

    result = pv.list_projects_with_chains(ttl=60.0)
    entry = next(e for e in result if e["project"] == str(project))
    (chain,) = entry["chains"]
    # Registry still shows c1, but presence was filtered out, so focus is empty.
    assert chain["chain_id"] == "c1"
    assert chain["focus"] == ""
    assert chain["has_presence"] is False


# ---------- get_project_detail ----------


def test_get_project_detail_empty(reg_patch, tmp_path):
    project = tmp_path / "p"
    project.mkdir()
    detail = pv.get_project_detail(project)
    assert detail["project"] == str(project)
    assert detail["chains"] == []
    assert detail["claims"] == {}
    assert detail["activity"] == []


def test_get_project_detail_with_claims(reg_patch, tmp_path):
    project = tmp_path / "p"
    project.mkdir()
    _register("c1", str(project))
    pc.register_chain(project, "c1", focus="tests")
    ok, conflicts = pc.claim_files(project, "c1", ["tests/test_foo.py"])
    assert ok and not conflicts

    detail = pv.get_project_detail(project)
    assert detail["claims"] == {"tests/test_foo.py": "c1"}
    assert any(
        a["kind"] == "claim" and a["chain_id"] == "c1"
        for a in detail["activity"]
    )


def test_get_project_detail_no_project_sentinel(reg_patch):
    """Passing an empty / None-ish value returns the (no project) bucket view."""
    _register("orphan", None)
    detail = pv.get_project_detail("")
    assert detail["project"] == pv.NO_PROJECT_KEY
    # Orphan chains live in the sentinel bucket; claims/activity come from
    # project coordination which doesn't apply to a no-project chain.
    ids = {c["chain_id"] for c in detail["chains"]}
    assert "orphan" in ids
    assert detail["claims"] == {}
    assert detail["activity"] == []


def test_get_project_detail_handles_unreadable_coordination(
    reg_patch, tmp_path, monkeypatch
):
    """A coordinator read blowing up doesn't crash the detail call."""
    project = tmp_path / "p"
    project.mkdir()
    _register("c1", str(project))

    def boom(*args, **kwargs):
        raise OSError("coord unreadable")

    monkeypatch.setattr(pc, "list_chains", boom)
    detail = pv.get_project_detail(project)
    # Registry side still populates; presence side degrades to empty.
    assert len(detail["chains"]) == 1
    assert detail["claims"] == {}


# ---------- project_summary_text ----------


def test_project_summary_text_no_project_returns_friendly_message(reg_patch):
    assert "no project" in pv.project_summary_text("").lower()


def test_project_summary_text_passes_through(reg_patch, tmp_path):
    project = tmp_path / "p"
    project.mkdir()
    _register("c1", str(project))
    pc.register_chain(project, "c1", focus="ui")
    text = pv.project_summary_text(project)
    assert "c1" in text
    assert "ui" in text


# ---------- conflict surfacing ----------


def _plant_claim_overlap(project: Path, file_path: str, chain_ids: list[str]):
    """Directly rewrite coordination.json so multiple chains claim one file.

    `project_coordinator.claim_files()` refuses to add a claim held by a
    live peer, so the only way to produce a genuine overlap for tests is
    to bypass it. That's what the conflict-surfacing code is for — catching
    state that shouldn't normally exist but does.
    """
    import json as _json
    coord = pc.coordination_path(project)
    data = _json.loads(coord.read_text())
    for cid in chain_ids:
        files = data["chains"][cid].setdefault("files_claimed", [])
        if file_path not in files:
            files.append(file_path)
    coord.write_text(_json.dumps(data))


def test_list_project_conflicts_empty(reg_patch, tmp_path):
    """No coordination file / no claims → no conflicts."""
    project = tmp_path / "p"
    project.mkdir()
    assert pv.list_project_conflicts(project) == []


def test_list_project_conflicts_no_project_sentinel(reg_patch):
    """NO_PROJECT_KEY has no coordination file and thus can't have conflicts."""
    assert pv.list_project_conflicts("") == []


def test_list_project_conflicts_disjoint_claims(reg_patch, tmp_path):
    """Two chains claiming different files is not a conflict."""
    project = tmp_path / "p"
    project.mkdir()
    _register("a", str(project))
    _register("b", str(project))
    pc.register_chain(project, "a", focus="api")
    pc.register_chain(project, "b", focus="tests")
    pc.claim_files(project, "a", ["src/api.py"])
    pc.claim_files(project, "b", ["tests/test_api.py"])
    assert pv.list_project_conflicts(project) == []


def test_list_project_conflicts_detects_overlap(reg_patch, tmp_path):
    """Two live chains with the same file in files_claimed surfaces a conflict."""
    project = tmp_path / "p"
    project.mkdir()
    _register("a", str(project))
    _register("b", str(project))
    pc.register_chain(project, "a", focus="api")
    pc.register_chain(project, "b", focus="api-rewrite")
    pc.claim_files(project, "a", ["src/api.py"])
    _plant_claim_overlap(project, "src/api.py", ["b"])

    conflicts = pv.list_project_conflicts(project)
    assert len(conflicts) == 1
    assert conflicts[0]["file"] == "src/api.py"
    assert conflicts[0]["chains"] == ["a", "b"]


def test_list_project_conflicts_ignores_stale(reg_patch, tmp_path):
    """A stale chain's claim doesn't count — it's effectively gone."""
    project = tmp_path / "p"
    project.mkdir()
    _register("a", str(project))
    _register("b", str(project))
    pc.register_chain(project, "a", focus="api")
    pc.register_chain(project, "b", focus="api-rewrite")
    pc.claim_files(project, "a", ["src/api.py"])
    _plant_claim_overlap(project, "src/api.py", ["b"])

    # Force chain a's presence to be ancient — ttl filter drops it.
    import json as _json
    coord = pc.coordination_path(project)
    data = _json.loads(coord.read_text())
    data["chains"]["a"]["last_seen"] = 0.0
    coord.write_text(_json.dumps(data))

    assert pv.list_project_conflicts(project, ttl=60.0) == []


def test_list_project_conflicts_handles_unreadable(
    reg_patch, tmp_path, monkeypatch,
):
    """A coordinator read blowing up degrades to an empty conflicts list."""
    project = tmp_path / "p"
    project.mkdir()

    def boom(*args, **kwargs):
        raise OSError("coord unreadable")

    monkeypatch.setattr(pc, "list_chains", boom)
    assert pv.list_project_conflicts(project) == []


def test_list_projects_with_chains_carries_conflict_count(reg_patch, tmp_path):
    """conflict_count in the project entry counts files with 2+ claimants."""
    project = tmp_path / "p"
    project.mkdir()
    _register("a", str(project))
    _register("b", str(project))
    pc.register_chain(project, "a", focus="x")
    pc.register_chain(project, "b", focus="y")
    pc.claim_files(project, "a", ["src/one.py", "src/two.py"])
    _plant_claim_overlap(project, "src/one.py", ["b"])
    _plant_claim_overlap(project, "src/two.py", ["b"])

    entry = next(
        e for e in pv.list_projects_with_chains() if e["project"] == str(project)
    )
    assert entry["conflict_count"] == 2
    by_id = {c["chain_id"]: c for c in entry["chains"]}
    assert by_id["a"]["in_conflict"] is True
    assert by_id["b"]["in_conflict"] is True


def test_list_projects_with_chains_no_conflicts_sets_zero(reg_patch, tmp_path):
    """Default conflict_count is 0 and in_conflict is False when nothing overlaps."""
    project = tmp_path / "p"
    project.mkdir()
    _register("a", str(project))
    pc.register_chain(project, "a", focus="x")
    pc.claim_files(project, "a", ["src/one.py"])

    entry = next(
        e for e in pv.list_projects_with_chains() if e["project"] == str(project)
    )
    assert entry["conflict_count"] == 0
    (chain,) = entry["chains"]
    assert chain["in_conflict"] is False


def test_get_project_detail_includes_conflicts_key(reg_patch, tmp_path):
    """Even projects without conflicts carry `conflicts: []` in the detail payload."""
    project = tmp_path / "p"
    project.mkdir()
    _register("a", str(project))
    pc.register_chain(project, "a", focus="x")

    detail = pv.get_project_detail(project)
    assert detail["conflicts"] == []
    # Chain also flags as in_conflict=False
    (chain,) = detail["chains"]
    assert chain["in_conflict"] is False


def test_get_project_detail_surfaces_overlap(reg_patch, tmp_path):
    """Detail payload exposes the overlap + marks affected chains in_conflict."""
    project = tmp_path / "p"
    project.mkdir()
    _register("a", str(project))
    _register("b", str(project))
    pc.register_chain(project, "a", focus="x")
    pc.register_chain(project, "b", focus="y")
    pc.claim_files(project, "a", ["src/api.py"])
    _plant_claim_overlap(project, "src/api.py", ["b"])

    detail = pv.get_project_detail(project)
    assert detail["conflicts"] == [
        {"file": "src/api.py", "chains": ["a", "b"]},
    ]
    flags = {c["chain_id"]: c["in_conflict"] for c in detail["chains"]}
    assert flags == {"a": True, "b": True}


# ---------- claim_rate surfacing (ui_claim_rate_surfacing, this segment) ----------


def test_list_projects_with_chains_carries_claim_rate_defaults(reg_patch, tmp_path):
    """Chains with no claim activity get recent_claims=0, total=0."""
    project = tmp_path / "p"
    project.mkdir()
    _register("a", str(project))
    pc.register_chain(project, "a", focus="x")

    entry = next(
        e for e in pv.list_projects_with_chains() if e["project"] == str(project)
    )
    assert entry["recent_claim_total"] == 0
    assert entry["claim_rate_window_seconds"] == pv.CLAIM_RATE_WINDOW_DEFAULT
    (chain,) = entry["chains"]
    assert chain["recent_claims"] == 0


def test_list_projects_with_chains_counts_claim_activity(reg_patch, tmp_path):
    """Each `claim_files` call logs one claim entry; per-chain counts match."""
    project = tmp_path / "p"
    project.mkdir()
    _register("a", str(project))
    _register("b", str(project))
    pc.register_chain(project, "a", focus="api")
    pc.register_chain(project, "b", focus="tests")
    # a claims twice, b once. Claiming an already-claimed file still logs the
    # CLI invocation, so hitting src/api.py twice counts as two claim entries.
    pc.claim_files(project, "a", ["src/api.py"])
    pc.claim_files(project, "a", ["src/models.py"])
    pc.claim_files(project, "b", ["tests/test_api.py"])

    entry = next(
        e for e in pv.list_projects_with_chains() if e["project"] == str(project)
    )
    by_id = {c["chain_id"]: c for c in entry["chains"]}
    assert by_id["a"]["recent_claims"] == 2
    assert by_id["b"]["recent_claims"] == 1
    assert entry["recent_claim_total"] == 3


def test_list_projects_with_chains_claim_rate_window_passes_through(reg_patch, tmp_path):
    """A tiny window must filter out older claim entries.

    The test plants a claim entry with a timestamp from two hours ago, then
    passes window_seconds=60. claim_rate should see an empty window and the
    merged chain dict should report recent_claims=0.
    """
    import json as _json
    import time as _time

    project = tmp_path / "p"
    project.mkdir()
    _register("a", str(project))
    pc.register_chain(project, "a", focus="x")
    pc.claim_files(project, "a", ["src/api.py"])

    # Backdate the single claim activity entry so a short window excludes it.
    coord = pc.coordination_path(project)
    data = _json.loads(coord.read_text())
    for act in data["activity"]:
        if act.get("kind") == "claim":
            act["timestamp"] = _time.time() - 7200
    coord.write_text(_json.dumps(data))

    entry = next(
        e for e in pv.list_projects_with_chains(claim_rate_window=60.0)
        if e["project"] == str(project)
    )
    assert entry["recent_claim_total"] == 0
    assert entry["claim_rate_window_seconds"] == 60.0
    (chain,) = entry["chains"]
    assert chain["recent_claims"] == 0


def test_list_projects_with_chains_claim_rate_survives_read_failure(
    reg_patch, tmp_path, monkeypatch,
):
    """An OSError from claim_rate must leave the project visible with zero counts."""
    project = tmp_path / "p"
    project.mkdir()
    _register("a", str(project))
    pc.register_chain(project, "a", focus="x")
    pc.claim_files(project, "a", ["src/api.py"])

    def boom(*args, **kwargs):
        raise OSError("coord claim_rate unreadable")

    monkeypatch.setattr(pc, "claim_rate", boom)
    entry = next(
        e for e in pv.list_projects_with_chains() if e["project"] == str(project)
    )
    # Project still renders. Per-chain counts fall back to 0.
    assert entry["recent_claim_total"] == 0
    (chain,) = entry["chains"]
    assert chain["recent_claims"] == 0


def test_list_projects_no_project_bucket_has_zero_claim_rate(reg_patch):
    """The NO_PROJECT_KEY bucket has no coordination file; claim fields stay at 0."""
    _register("solo", None)
    (entry,) = pv.list_projects_with_chains()
    assert entry["project"] == pv.NO_PROJECT_KEY
    assert entry["recent_claim_total"] == 0
    (chain,) = entry["chains"]
    assert chain["recent_claims"] == 0


def test_get_project_detail_carries_claim_rates(reg_patch, tmp_path):
    """Detail payload exposes the claim_rates dict and per-chain recent_claims."""
    project = tmp_path / "p"
    project.mkdir()
    _register("a", str(project))
    _register("b", str(project))
    pc.register_chain(project, "a", focus="x")
    pc.register_chain(project, "b", focus="y")
    pc.claim_files(project, "a", ["src/api.py"])
    pc.claim_files(project, "a", ["src/models.py"])

    detail = pv.get_project_detail(project)
    assert detail["claim_rates"] == {"a": 2}
    assert detail["claim_rate_window_seconds"] == pv.CLAIM_RATE_WINDOW_DEFAULT
    by_id = {c["chain_id"]: c["recent_claims"] for c in detail["chains"]}
    assert by_id == {"a": 2, "b": 0}


def test_get_project_detail_claim_rates_window_passes_through(reg_patch, tmp_path):
    """Passing a short window narrows the result to entries inside it."""
    import json as _json
    import time as _time

    project = tmp_path / "p"
    project.mkdir()
    _register("a", str(project))
    pc.register_chain(project, "a", focus="x")
    pc.claim_files(project, "a", ["src/api.py"])

    coord = pc.coordination_path(project)
    data = _json.loads(coord.read_text())
    for act in data["activity"]:
        if act.get("kind") == "claim":
            act["timestamp"] = _time.time() - 7200
    coord.write_text(_json.dumps(data))

    detail = pv.get_project_detail(project, claim_rate_window=60.0)
    assert detail["claim_rates"] == {}
    assert detail["claim_rate_window_seconds"] == 60.0
    (chain,) = detail["chains"]
    assert chain["recent_claims"] == 0


def test_list_projects_presence_only_chain_gets_claim_rate(reg_patch, tmp_path):
    """A chain that only exists in the coordination file (no registry row)
    still gets its recent_claims counted."""
    project = tmp_path / "p"
    project.mkdir()
    # NB: no _register() — 'ghost' only exists in coordination.
    pc.register_chain(project, "ghost", focus="z")
    pc.claim_files(project, "ghost", ["src/ghost.py"])

    # Also register a real chain on the project so the bucket exists in the
    # registry groupby. Without this, the project wouldn't appear at all.
    _register("real", str(project))
    pc.register_chain(project, "real", focus="real")

    entry = next(
        e for e in pv.list_projects_with_chains() if e["project"] == str(project)
    )
    by_id = {c["chain_id"]: c["recent_claims"] for c in entry["chains"]}
    assert by_id.get("ghost") == 1
    assert by_id.get("real") == 0
    assert entry["recent_claim_total"] == 1
