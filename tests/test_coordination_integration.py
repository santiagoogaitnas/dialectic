"""Cross-module integration tests for the coordination stack.

The unit tests in ``test_project_coordinator``, ``test_project_view``, and
``test_directory_browser`` pin each module in isolation. What they can't
verify is how the three compose with ``registry.py`` once multiple chains
are live on the same project:

- A dashboard snapshot has to blend the process-side registry with the
  project-side coordination file — stale in one store, fresh in the
  other, conflicting writes between them.
- The directory-picker pipeline (``browse`` → ``validate_project_dir`` →
  register chain → surface it in ``list_projects_with_chains``) is a
  real user journey the current tests never exercise end-to-end.
- Claim / release / stale-eviction has to stay consistent across both
  stores so ``get_project_detail`` and ``project_summary_text`` tell
  the same story.

Every test here uses real temp directories so the fcntl locking, atomic
writes, and directory walks run against an actual filesystem.
"""

from __future__ import annotations

import json
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
from ui import directory_browser as db
from ui import project_view as pv


# --- Fixtures + helpers ---


@pytest.fixture
def isolated_registry(tmp_path):
    """Redirect the registry file + workspace into tmp_path for test isolation.

    The registry normally lives at ``chainwork/.registry.json`` in the repo.
    Pointing it at a tmp path means parallel test runs don't trample each
    other and an aborted test can't leave state behind in the real registry.
    """
    reg_file = tmp_path / ".registry.json"
    with patch.object(reg, "REGISTRY_FILE", reg_file), \
         patch.object(reg, "WORKSPACE", tmp_path):
        yield tmp_path


def _register_in_registry(chain_id: str, project: str | None,
                          seed: str = "seed", pid: int | None = None) -> None:
    """Add a row to the (patched) registry. Live PID by default."""
    cfg = ChainConfig(
        chain_id=chain_id, session=f"sess-{chain_id}",
        seed=seed, project=project,
    )
    reg.register_chain(cfg, pid if pid is not None else os.getpid())


def _mk_project(tmp_path: Path, name: str) -> Path:
    d = tmp_path / name
    d.mkdir()
    return d


# ---------- Realistic multi-chain workflows ----------


def test_three_chains_with_distinct_focus_areas_and_claims(
    isolated_registry, tmp_path,
):
    """The split-focus scenario from the README: three chains, three focus
    areas, three disjoint claim sets on one project. Every chain should
    surface in the grouped view with its own focus and own claims.
    """
    project = _mk_project(tmp_path, "app")

    _register_in_registry("backend", str(project))
    _register_in_registry("tests", str(project))
    _register_in_registry("frontend", str(project))

    pc.register_chain(project, "backend", focus="backend api")
    pc.register_chain(project, "tests", focus="test coverage")
    pc.register_chain(project, "frontend", focus="react ui")

    pc.claim_files(project, "backend", ["src/api/auth.py", "src/api/users.py"])
    pc.claim_files(project, "tests", ["tests/test_auth.py"])
    pc.claim_files(project, "frontend", ["web/src/Login.tsx", "web/src/App.tsx"])

    result = pv.list_projects_with_chains()
    assert len(result) == 1
    entry = result[0]
    assert entry["project"] == str(project)
    assert entry["chain_count"] == 3
    assert entry["active_count"] == 3
    assert set(entry["focus_areas"]) == {"backend api", "test coverage", "react ui"}

    # The three chains are distinguishable by id and focus, and their claims
    # are disjoint so a reader can tell who's working where without the file
    # being duplicated across chain rows.
    by_id = {c["chain_id"]: c for c in entry["chains"]}
    assert by_id["backend"]["focus"] == "backend api"
    assert by_id["tests"]["focus"] == "test coverage"
    assert by_id["frontend"]["focus"] == "react ui"
    assert set(by_id["backend"]["files_claimed"]) == {
        "src/api/auth.py", "src/api/users.py",
    }
    assert by_id["tests"]["files_claimed"] == ["tests/test_auth.py"]
    assert set(by_id["frontend"]["files_claimed"]) == {
        "web/src/Login.tsx", "web/src/App.tsx",
    }


def test_chain_lifecycle_register_claim_release_deregister(
    isolated_registry, tmp_path,
):
    """Walk one chain through register → claim → release → deregister and
    verify project_view + project_coordinator agree at every step.
    """
    project = _mk_project(tmp_path, "proj")

    # 1. Register in both stores.
    _register_in_registry("c1", str(project))
    pc.register_chain(project, "c1", focus="initial")
    snapshot = pv.list_projects_with_chains()
    (entry,) = snapshot
    (chain,) = entry["chains"]
    assert chain["has_registry"] is True
    assert chain["has_presence"] is True
    assert chain["files_claimed"] == []

    # 2. Claim two files. file_owner() and get_project_detail agree.
    ok, conflicts = pc.claim_files(project, "c1", ["a.py", "b.py"])
    assert ok and not conflicts
    assert pc.file_owner(project, "a.py") == "c1"
    detail = pv.get_project_detail(project)
    assert detail["claims"] == {"a.py": "c1", "b.py": "c1"}

    # 3. Release just one file. The other stays owned.
    released = pc.release_files(project, "c1", ["a.py"])
    assert released == 1
    assert pc.file_owner(project, "a.py") is None
    assert pc.file_owner(project, "b.py") == "c1"
    detail = pv.get_project_detail(project)
    assert detail["claims"] == {"b.py": "c1"}

    # 4. Deregister from coordination. Registry still has the row so the
    # chain doesn't vanish from the dashboard, but presence is gone.
    assert pc.deregister_chain(project, "c1") is True
    snapshot = pv.list_projects_with_chains()
    (entry,) = snapshot
    (chain,) = entry["chains"]
    assert chain["has_registry"] is True
    assert chain["has_presence"] is False
    assert chain["focus"] == ""
    assert chain["files_claimed"] == []


def test_two_projects_are_coordinated_independently(
    isolated_registry, tmp_path,
):
    """Chains on different projects can't see each other's coordination
    files. file_owner on proj1 must not leak to proj2.
    """
    proj1 = _mk_project(tmp_path, "alpha")
    proj2 = _mk_project(tmp_path, "beta")

    _register_in_registry("a1", str(proj1))
    _register_in_registry("b1", str(proj2))
    pc.register_chain(proj1, "a1", focus="alpha-work")
    pc.register_chain(proj2, "b1", focus="beta-work")
    pc.claim_files(proj1, "a1", ["shared_name.py"])
    pc.claim_files(proj2, "b1", ["shared_name.py"])

    # Same filename in both projects is fine — they're not the same file.
    assert pc.file_owner(proj1, "shared_name.py") == "a1"
    assert pc.file_owner(proj2, "shared_name.py") == "b1"

    result = pv.list_projects_with_chains()
    projects = {e["project"] for e in result}
    assert projects == {str(proj1), str(proj2)}
    for entry in result:
        assert entry["chain_count"] == 1


# ---------- Cross-store divergence ----------


def test_registry_chain_with_no_presence_yet(isolated_registry, tmp_path):
    """Freshly registered chain that hasn't hit project_coordinator yet still
    appears in the grouped view — with flagged empty coordination fields.
    """
    project = _mk_project(tmp_path, "fresh")
    _register_in_registry("c1", str(project))
    (entry,) = pv.list_projects_with_chains()
    (chain,) = entry["chains"]
    assert chain["has_registry"] is True
    assert chain["has_presence"] is False
    assert chain["focus"] == ""
    assert chain["files_claimed"] == []


def test_presence_without_matching_registry_row_surfaces_ghost(
    isolated_registry, tmp_path,
):
    """A coordination file can outlive the registry row (crash, manual
    cleanup). The ghost chain shows up so the operator notices.
    """
    project = _mk_project(tmp_path, "ghosty")
    _register_in_registry("alive", str(project))
    pc.register_chain(project, "alive", focus="alive-focus")
    pc.register_chain(project, "ghost", focus="ghost-focus")

    (entry,) = pv.list_projects_with_chains()
    ids = {c["chain_id"] for c in entry["chains"]}
    assert ids == {"alive", "ghost"}
    ghost = next(c for c in entry["chains"] if c["chain_id"] == "ghost")
    assert ghost["has_registry"] is False
    assert ghost["has_presence"] is True
    assert ghost["focus"] == "ghost-focus"


def test_dead_registry_chain_still_reports_through_project_view(
    isolated_registry, tmp_path,
):
    """A registry row with a dead PID flips to ``status='dead'`` on the next
    list_chains() call. The project view still reports it so the operator
    can prune it, and presence coordination is unaffected.
    """
    project = _mk_project(tmp_path, "zombie")
    # PID=1 is init on Unix — not ours, os.kill(1,0) succeeds from root but
    # raises PermissionError for non-root, which list_chains() does not
    # consider dead. We need a truly-dead pid instead. Pick a high pid
    # that's extremely unlikely to be running.
    _register_in_registry("zomb", str(project), pid=2**30)
    pc.register_chain(project, "zomb", focus="still-here")

    (entry,) = pv.list_projects_with_chains()
    (chain,) = entry["chains"]
    # The dead PID reshapes the status...
    assert chain["status"] == "dead"
    # ...but the presence info is untouched. This is important: the operator
    # needs to see the focus the chain had so they know what was in flight.
    assert chain["focus"] == "still-here"
    # 'dead' counts as not-active for the grouped header.
    assert entry["active_count"] == 0


# ---------- Claim conflict + resolution ----------


def test_claim_conflict_preserves_original_owner(
    isolated_registry, tmp_path,
):
    """When chain B tries to claim a file chain A already owns (and A is
    live), the conflict is reported and A's claim is untouched. The
    project view must reflect A's ownership, not B's attempt.
    """
    project = _mk_project(tmp_path, "contested")
    _register_in_registry("a", str(project))
    _register_in_registry("b", str(project))
    pc.register_chain(project, "a", focus="first")
    pc.register_chain(project, "b", focus="second")

    pc.claim_files(project, "a", ["contested.py"])
    ok, conflicts = pc.claim_files(project, "b", ["contested.py"])
    assert ok is False
    assert conflicts == ["contested.py"]

    # A still owns it; B's claim set is empty.
    assert pc.file_owner(project, "contested.py") == "a"
    detail = pv.get_project_detail(project)
    assert detail["claims"] == {"contested.py": "a"}
    by_id = {c["chain_id"]: c for c in detail["chains"]}
    assert by_id["a"]["files_claimed"] == ["contested.py"]
    assert by_id["b"]["files_claimed"] == []


def test_stale_owner_claim_transferred_silently(
    isolated_registry, tmp_path,
):
    """A claim held by a chain whose presence is older than ttl should
    silently transfer to a new claimant — otherwise a crashed chain
    deadlocks every file it touched.
    """
    project = _mk_project(tmp_path, "stale")
    _register_in_registry("a", str(project))
    _register_in_registry("b", str(project))
    pc.register_chain(project, "a", focus="orig")
    pc.register_chain(project, "b", focus="taker")
    pc.claim_files(project, "a", ["target.py"])

    # Artificially age A's presence past any reasonable ttl.
    coord = pc.coordination_path(project)
    data = json.loads(coord.read_text())
    data["chains"]["a"]["last_seen"] = 0.0
    coord.write_text(json.dumps(data))

    ok, conflicts = pc.claim_files(project, "b", ["target.py"], ttl=60.0)
    assert ok is True
    assert conflicts == []

    # file_owner with the same ttl reports B now owns it. A's stale row
    # still exists in the JSON but its claim was dropped.
    assert pc.file_owner(project, "target.py", ttl=60.0) == "b"


def test_file_owner_lookup_matches_project_view_claims(
    isolated_registry, tmp_path,
):
    """``file_owner`` and ``get_project_detail['claims']`` must agree —
    they're two views of the same underlying state.
    """
    project = _mk_project(tmp_path, "agree")
    _register_in_registry("x", str(project))
    _register_in_registry("y", str(project))
    pc.register_chain(project, "x", focus="x-focus")
    pc.register_chain(project, "y", focus="y-focus")
    pc.claim_files(project, "x", ["src/one.py", "src/two.py"])
    pc.claim_files(project, "y", ["docs/readme.md"])

    detail = pv.get_project_detail(project)
    expected_claims = {
        "src/one.py": "x",
        "src/two.py": "x",
        "docs/readme.md": "y",
    }
    assert detail["claims"] == expected_claims
    for path, expected_owner in expected_claims.items():
        assert pc.file_owner(project, path) == expected_owner


# ---------- Directory browser → registration handoff ----------


def test_browse_validate_register_full_handoff(isolated_registry, tmp_path):
    """User journey: browse a directory, pick a subfolder, validate it,
    register a chain against it, verify it shows up in the grouped view.
    """
    # Layout tmp_path as if it held several projects.
    for name in ("proj-a", "proj-b", "unrelated"):
        (tmp_path / name).mkdir()
    # Make proj-a look like a project root via a marker file.
    (tmp_path / "proj-a" / "pyproject.toml").write_text("[project]\n")

    # 1. Browse — proj-a gets flagged as a project root, sorted first.
    listing = db.browse(tmp_path)
    assert listing["error"] is None
    names = [e["name"] for e in listing["entries"]]
    assert "proj-a" in names
    proj_a_entry = next(e for e in listing["entries"] if e["name"] == "proj-a")
    assert proj_a_entry["is_project"] is True

    # 2. Validate the picked path.
    picked = proj_a_entry["path"]
    ok, reason = db.validate_project_dir(picked)
    assert ok is True and reason == ""

    # 3. Use it as the project for a fresh chain in both stores.
    _register_in_registry("first", picked)
    pc.register_chain(Path(picked), "first", focus="initial work")

    # 4. Grouped view surfaces it under the exact path we picked.
    result = pv.list_projects_with_chains()
    by_key = {e["project"]: e for e in result}
    assert picked in by_key
    assert by_key[picked]["chain_count"] == 1
    (chain,) = by_key[picked]["chains"]
    assert chain["focus"] == "initial work"


def test_dialectic_dir_is_hidden_in_browser_after_registration(
    isolated_registry, tmp_path,
):
    """Once a chain registers on a project, ``.dialectic/`` exists. The
    picker should hide it by default (dotfile) so it doesn't clutter the
    picker UI for users picking a subdirectory to work on.
    """
    project = _mk_project(tmp_path, "withdialectic")
    pc.register_chain(project, "c1", focus="anything")
    assert (project / ".dialectic").is_dir()

    # Default browse omits dotdirs.
    default = db.browse(project)
    names = {e["name"] for e in default["entries"]}
    assert ".dialectic" not in names

    # With include_hidden=True, it shows.
    with_hidden = db.browse(project, include_hidden=True)
    hidden_names = {e["name"] for e in with_hidden["entries"]}
    assert ".dialectic" in hidden_names


def test_refused_system_path_never_reaches_coordinator(
    isolated_registry,
):
    """``validate_project_dir`` is a foot-gun guard. A caller that honors
    its verdict will never pass ``/etc`` to project_coordinator. We verify
    the guard fires; we don't verify coordinator behavior on bad input
    because the contract is "validated paths only."
    """
    ok, reason = db.validate_project_dir("/etc")
    assert ok is False
    assert "Refusing system path" in reason


def test_suggestions_paths_are_browsable_and_safe(isolated_registry):
    """``suggestions()`` is what populates the picker's "start here" list.
    Every path it returns should pass is_safe_path and not crash browse().
    """
    for suggested in db.suggestions():
        assert db.is_safe_path(suggested), (
            f"suggestion {suggested!r} is not safe"
        )
        listing = db.browse(suggested)
        # Either the browse succeeds (error=None) or it reports a readable
        # error string — never a raw exception bubbling through.
        assert listing["error"] is None or isinstance(listing["error"], str)


# ---------- TTL / stale cleanup across stack ----------


def test_release_stale_evicts_presence_registry_untouched(
    isolated_registry, tmp_path,
):
    """release_stale() is project-scoped. It must not mutate the registry,
    which is process-scoped and has its own liveness check.
    """
    project = _mk_project(tmp_path, "staleproj")
    _register_in_registry("c1", str(project))
    pc.register_chain(project, "c1", focus="going-stale")

    # Age the presence past the ttl we'll use.
    coord = pc.coordination_path(project)
    data = json.loads(coord.read_text())
    data["chains"]["c1"]["last_seen"] = 0.0
    coord.write_text(json.dumps(data))

    removed = pc.release_stale(project, ttl=60.0)
    assert removed == ["c1"]

    # Registry still shows the chain (PID is live, so status=running).
    records = reg.list_chains()
    assert len(records) == 1
    assert records[0].chain_id == "c1"
    assert records[0].status == "running"

    # Project view: registry has it, presence is gone.
    (entry,) = pv.list_projects_with_chains()
    (chain,) = entry["chains"]
    assert chain["has_registry"] is True
    assert chain["has_presence"] is False


def test_list_projects_ttl_zero_shows_every_presence(
    isolated_registry, tmp_path,
):
    """ttl<=0 is the "don't filter" escape hatch. Every presence should
    surface regardless of age so operators can see the full history.
    """
    project = _mk_project(tmp_path, "notail")
    _register_in_registry("fresh", str(project))
    _register_in_registry("old", str(project))
    pc.register_chain(project, "fresh", focus="now")
    pc.register_chain(project, "old", focus="long ago")

    # Age the 'old' presence way back.
    coord = pc.coordination_path(project)
    data = json.loads(coord.read_text())
    data["chains"]["old"]["last_seen"] = 0.0
    coord.write_text(json.dumps(data))

    # With a tight ttl, 'old' is filtered out of the presence side — but its
    # registry row still surfaces it, just with has_presence=False.
    result_tight = pv.list_projects_with_chains(ttl=60.0)
    (entry,) = result_tight
    by_id = {c["chain_id"]: c for c in entry["chains"]}
    assert by_id["old"]["has_presence"] is False
    assert by_id["fresh"]["has_presence"] is True

    # With ttl=0, 'old' gets its presence back.
    result_all = pv.list_projects_with_chains(ttl=0.0)
    (entry_all,) = result_all
    by_id_all = {c["chain_id"]: c for c in entry_all["chains"]}
    assert by_id_all["old"]["has_presence"] is True
    assert by_id_all["fresh"]["has_presence"] is True


# ---------- Summary text + detail aggregation ----------


def test_project_summary_text_reflects_multi_chain_state(
    isolated_registry, tmp_path,
):
    """project_summary_text is what agents quote at each other at reset
    time to know who's working on what. All live chains, their focus,
    and their claims must appear in the rendered text.
    """
    project = _mk_project(tmp_path, "summable")
    _register_in_registry("api", str(project))
    _register_in_registry("ui", str(project))
    pc.register_chain(project, "api", focus="server endpoints")
    pc.register_chain(project, "ui", focus="react forms")
    pc.claim_files(project, "api", ["server/routes.py"])
    pc.claim_files(project, "ui", ["web/forms/LoginForm.tsx"])

    text = pv.project_summary_text(project)
    for needle in (
        "api", "ui",
        "server endpoints", "react forms",
        "server/routes.py", "web/forms/LoginForm.tsx",
    ):
        assert needle in text, f"expected {needle!r} in summary:\n{text}"


def test_get_project_detail_aggregates_claims_activity_and_chains(
    isolated_registry, tmp_path,
):
    """get_project_detail is the per-project drilldown. It must return a
    coherent snapshot: every registered chain, every live claim, and a
    chronological activity log covering the register + claim operations.
    """
    project = _mk_project(tmp_path, "detailproj")
    _register_in_registry("alpha", str(project))
    _register_in_registry("beta", str(project))
    pc.register_chain(project, "alpha", focus="area-a")
    pc.register_chain(project, "beta", focus="area-b")
    pc.claim_files(project, "alpha", ["src/alpha.py"])
    pc.claim_files(project, "beta", ["src/beta.py", "src/shared.py"])
    pc.append_note(project, "alpha", "landed first review pass")

    detail = pv.get_project_detail(project)
    assert detail["project"] == str(project)

    # Chains — both registry rows surface, with their focus.
    ids = {c["chain_id"]: c for c in detail["chains"]}
    assert set(ids) == {"alpha", "beta"}
    assert ids["alpha"]["focus"] == "area-a"
    assert ids["beta"]["focus"] == "area-b"

    # Claims — union of both chains' claims, one entry per file.
    assert detail["claims"] == {
        "src/alpha.py": "alpha",
        "src/beta.py": "beta",
        "src/shared.py": "beta",
    }

    # Activity — register + claim + note events for both chains.
    kinds_by_chain: dict[str, set[str]] = {}
    for a in detail["activity"]:
        kinds_by_chain.setdefault(a["chain_id"], set()).add(a["kind"])
    assert "register" in kinds_by_chain["alpha"]
    assert "claim" in kinds_by_chain["alpha"]
    assert "note" in kinds_by_chain["alpha"]
    assert "register" in kinds_by_chain["beta"]
    assert "claim" in kinds_by_chain["beta"]


def test_no_project_bucket_summary_text_is_friendly(isolated_registry):
    """Chains registered without a project live in the NO_PROJECT_KEY
    bucket. project_summary_text must not try to read a nonexistent
    coordination file for them — it returns a friendly explanation.
    """
    _register_in_registry("orphan", None)
    text = pv.project_summary_text("")
    # Matches the friendly message, not an OSError stack.
    assert "no project" in text.lower()
    # Grouped view still shows the orphan bucket so operators can stop it.
    result = pv.list_projects_with_chains()
    (entry,) = result
    assert entry["project"] == pv.NO_PROJECT_KEY
    (chain,) = entry["chains"]
    assert chain["chain_id"] == "orphan"
