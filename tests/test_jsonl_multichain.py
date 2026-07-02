"""Multi-chain JSONL discovery isolation tests.

Fills a coverage gap: test_jsonl_reader.py exercises cursor/slug/read in
single-file scope, and test_integration_multichain.py exercises pane_dirs,
registry, and UI — but no test verifies that JSONL discovery itself stays
isolated when two chains share a project.

The isolation guarantee relies on each chain's pane_dir being unique (via
chain_id). Slugifying a unique pane_dir yields a unique ~/.claude/projects
subdirectory, so _discover_via_mtime cannot pick up another chain's JSONL
file. These tests pin that invariant.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

_project_root = str(Path(__file__).parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from janitor.jsonl_reader import (
    _discover_via_mtime,
    discover_jsonl_for_pane,
    slugify_project_path,
)
from registry import ChainConfig, generate_chain_id


# --- Slug isolation across chain pane_dirs ---


def test_slug_differs_for_two_chains_same_project():
    """Two chains on the same project must slugify to different paths."""
    project = Path("/Users/x/myproj")
    cfg1 = ChainConfig(chain_id="aa11bb22", session="s1", seed="x",
                       project=str(project))
    cfg2 = ChainConfig(chain_id="cc33dd44", session="s2", seed="x",
                       project=str(project))

    # pane_dirs returns chain-specific subdirs; simulate without mkdir
    dir_a_1 = project / f".dialectic-a-{cfg1.chain_id}"
    dir_a_2 = project / f".dialectic-a-{cfg2.chain_id}"

    slug_1 = slugify_project_path(dir_a_1)
    slug_2 = slugify_project_path(dir_a_2)

    assert slug_1 != slug_2
    assert cfg1.chain_id in slug_1
    assert cfg2.chain_id in slug_2


def test_slug_differs_for_chain_a_vs_chain_b_same_chain():
    """Within a single chain, pane A and pane B get different slugs."""
    project = Path("/Users/x/myproj")
    cfg = ChainConfig(chain_id="zz99yy88", session="s", seed="x",
                      project=str(project))
    dir_a = project / f".dialectic-a-{cfg.chain_id}"
    dir_b = project / f".dialectic-b-{cfg.chain_id}"

    slug_a = slugify_project_path(dir_a)
    slug_b = slugify_project_path(dir_b)

    assert slug_a != slug_b


def test_slug_unique_for_no_project_workspace_chains(tmp_path):
    """Chainwork fallback paths (chainwork/<chain_id>/a) slugify uniquely.

    Exercises the no-project ChainConfig path, where each chain's pane_dir
    is a chainwork subdir rather than a project-local .dialectic-a-<id>.
    """
    # Simulate chainwork/<chain_id>/a for two chains
    ws_root = tmp_path / "chainwork"
    ws_root.mkdir()
    dir_1 = ws_root / "chain1-id" / "a"
    dir_2 = ws_root / "chain2-id" / "a"

    slug_1 = slugify_project_path(dir_1)
    slug_2 = slugify_project_path(dir_2)

    assert slug_1 != slug_2
    assert "chain1-id" in slug_1
    assert "chain2-id" in slug_2


def test_slug_does_not_collide_across_generated_chain_ids():
    """Fuzz: 50 generated chain_ids all produce unique pane_dir slugs."""
    import time
    project = Path("/Users/x/myproj")
    slugs = set()
    for _ in range(50):
        cid = generate_chain_id()
        dir_a = project / f".dialectic-a-{cid}"
        slug = slugify_project_path(dir_a)
        assert slug not in slugs, f"collision: {slug}"
        slugs.add(slug)
        # generate_chain_id uses second-precision timestamp + hex;
        # the hex suffix is what guarantees uniqueness within a second.
        # (No sleep needed; uuid4().hex[:4] gives ~16-bit entropy.)
    assert len(slugs) == 50


def test_slug_handles_all_special_characters():
    """Periods, slashes, spaces, underscores all become dashes."""
    path = Path("/Users/x/my_project.v2/with space")
    slug = slugify_project_path(path)
    # No raw separators should survive
    assert "/" not in slug
    assert " " not in slug
    assert "_" not in slug
    assert "." not in slug
    assert slug.startswith("-")


# --- _discover_via_mtime isolation ---


def test_discover_via_mtime_scoped_to_given_dir(tmp_path):
    """_discover_via_mtime only sees JSONLs inside the dir passed in."""
    chain1_dir = tmp_path / "-chain1-slug"
    chain2_dir = tmp_path / "-chain2-slug"
    chain1_dir.mkdir()
    chain2_dir.mkdir()

    # Both chains have a JSONL file; chain2's is newer by mtime
    (chain1_dir / "uuid1.jsonl").write_text('{"type":"user"}\n')
    import time
    time.sleep(0.01)
    (chain2_dir / "uuid2.jsonl").write_text('{"type":"user"}\n')

    found_1 = _discover_via_mtime(chain1_dir)
    found_2 = _discover_via_mtime(chain2_dir)

    assert found_1 is not None
    assert found_2 is not None
    assert found_1.name == "uuid1.jsonl"
    assert found_2.name == "uuid2.jsonl"
    # Critical: chain 1's discovery must NOT return chain 2's newer file
    assert found_1.parent == chain1_dir
    assert found_2.parent == chain2_dir


def test_discover_via_mtime_returns_most_recent_within_chain(tmp_path):
    """Within one chain's slug dir, the newest file is returned.

    This is the /clear scenario: after a reset, Claude Code writes a new
    JSONL file into the same slug dir. We want the newer one.
    """
    import time
    chain_dir = tmp_path / "-single-chain"
    chain_dir.mkdir()
    old = chain_dir / "old-session.jsonl"
    new = chain_dir / "new-session.jsonl"
    old.write_text('{"type":"user"}\n')
    time.sleep(0.02)
    new.write_text('{"type":"user"}\n')

    found = _discover_via_mtime(chain_dir)
    assert found == new


def test_discover_via_mtime_empty_dir(tmp_path):
    empty = tmp_path / "-empty"
    empty.mkdir()
    assert _discover_via_mtime(empty) is None


def test_discover_via_mtime_ignores_non_jsonl(tmp_path):
    chain_dir = tmp_path / "-mixed"
    chain_dir.mkdir()
    (chain_dir / "notes.txt").write_text("not json")
    (chain_dir / "other.log").write_text("log")
    assert _discover_via_mtime(chain_dir) is None


# --- discover_jsonl_for_pane end-to-end (mocked ~/.claude) ---


def test_discover_jsonl_for_pane_isolates_two_chains(tmp_path, monkeypatch):
    """Two chains' pane_dirs resolve to independent JSONL files.

    Simulates: ~/.claude/projects/<slug>/<session>.jsonl for each chain,
    with Home patched to tmp_path. Discovery falls through lsof (fails
    silently with no tmux) into _discover_via_mtime.
    """
    fake_home = tmp_path / "home"
    projects_dir = fake_home / ".claude" / "projects"
    projects_dir.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

    project = tmp_path / "shared-project"
    project.mkdir()
    dir_chain_a = project / ".dialectic-a-chainAAA"
    dir_chain_b = project / ".dialectic-a-chainBBB"
    dir_chain_a.mkdir()
    dir_chain_b.mkdir()

    slug_a = slugify_project_path(dir_chain_a)
    slug_b = slugify_project_path(dir_chain_b)
    assert slug_a != slug_b

    (projects_dir / slug_a).mkdir()
    (projects_dir / slug_b).mkdir()
    jsonl_a = projects_dir / slug_a / "session-a.jsonl"
    jsonl_b = projects_dir / slug_b / "session-b.jsonl"
    jsonl_a.write_text('{"type":"user","message":{"content":"A"}}\n')
    jsonl_b.write_text('{"type":"user","message":{"content":"B"}}\n')

    # tmux/lsof unavailable in tests → lsof strategy fails, mtime fallback fires
    found_a = discover_jsonl_for_pane("no-such:0.0", dir_chain_a)
    found_b = discover_jsonl_for_pane("no-such:0.0", dir_chain_b)

    assert found_a == jsonl_a
    assert found_b == jsonl_b
    # Most important invariant: chain A never resolves to chain B's file
    assert found_a != found_b


def test_discover_jsonl_for_pane_missing_slug_dir_returns_none(tmp_path, monkeypatch):
    """If no ~/.claude/projects/<slug> dir exists yet, discovery returns None."""
    fake_home = tmp_path / "home"
    (fake_home / ".claude" / "projects").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

    project_dir = tmp_path / "never-logged-to"
    project_dir.mkdir()

    result = discover_jsonl_for_pane("no-such:0.0", project_dir)
    assert result is None


def test_discover_jsonl_for_pane_slug_dir_exists_but_empty(tmp_path, monkeypatch):
    """Slug dir exists (chain booted) but agent hasn't written yet → None."""
    fake_home = tmp_path / "home"
    projects_dir = fake_home / ".claude" / "projects"
    projects_dir.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

    pane_dir = tmp_path / "proj" / ".dialectic-a-empty1234"
    pane_dir.mkdir(parents=True)
    slug = slugify_project_path(pane_dir)
    (projects_dir / slug).mkdir()

    result = discover_jsonl_for_pane("no-such:0.0", pane_dir)
    assert result is None


def test_discover_jsonl_for_pane_picks_latest_in_chain_dir(tmp_path, monkeypatch):
    """Chain with multiple JSONLs (post-/clear case) resolves to the newest."""
    import time
    fake_home = tmp_path / "home"
    projects_dir = fake_home / ".claude" / "projects"
    projects_dir.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

    pane_dir = tmp_path / "proj" / ".dialectic-a-rollover1"
    pane_dir.mkdir(parents=True)
    slug = slugify_project_path(pane_dir)
    slug_dir = projects_dir / slug
    slug_dir.mkdir()

    (slug_dir / "old.jsonl").write_text('{"type":"user"}\n')
    time.sleep(0.02)
    (slug_dir / "new.jsonl").write_text('{"type":"user"}\n')

    result = discover_jsonl_for_pane("no-such:0.0", pane_dir)
    assert result is not None
    assert result.name == "new.jsonl"


# --- Cross-chain contamination regression ---


def test_writing_to_chain_a_does_not_affect_chain_b_discovery(tmp_path, monkeypatch):
    """Regression: a newer JSONL in chain A's slug dir must not leak into B.

    Pre-multi-chain (shared slug), if two chains wrote to the same slug dir,
    the mtime fallback would pick whichever wrote last — scrambling the
    dialogue. With per-chain slug dirs, this cannot happen.
    """
    import time
    fake_home = tmp_path / "home"
    projects_dir = fake_home / ".claude" / "projects"
    projects_dir.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

    project = tmp_path / "multi"
    project.mkdir()
    pane_dir_a = project / ".dialectic-a-AAAA"
    pane_dir_b = project / ".dialectic-a-BBBB"
    pane_dir_a.mkdir()
    pane_dir_b.mkdir()

    slug_a = slugify_project_path(pane_dir_a)
    slug_b = slugify_project_path(pane_dir_b)
    (projects_dir / slug_a).mkdir()
    (projects_dir / slug_b).mkdir()

    jsonl_a = projects_dir / slug_a / "a.jsonl"
    jsonl_b = projects_dir / slug_b / "b.jsonl"
    jsonl_a.write_text('{"type":"user","message":{"content":"original-A"}}\n')
    jsonl_b.write_text('{"type":"user","message":{"content":"original-B"}}\n')

    # Chain A continues writing (later mtime). Chain B should still resolve
    # to its own, older file — not A's newer one.
    time.sleep(0.02)
    jsonl_a.write_text(
        '{"type":"user","message":{"content":"original-A"}}\n'
        '{"type":"user","message":{"content":"updated-A"}}\n'
    )

    found_b = discover_jsonl_for_pane("no-such:0.0", pane_dir_b)
    assert found_b == jsonl_b
    assert "original-B" in found_b.read_text()
    assert "updated-A" not in found_b.read_text()


def test_pane_dir_based_slug_matches_claude_code_convention():
    """Slug format must stay compatible with Claude Code's path → dir mapping.

    Claude Code stores JSONL at ~/.claude/projects/<slug>/<session>.jsonl,
    where <slug> = cwd with /, space, _, . all replaced by -. Our pane_dirs
    live inside the project, so the slug they produce is what Claude Code
    will actually create when the agent starts there.
    """
    pane_dir = Path("/Users/alice/proj/.dialectic-a-04161138-abcd")
    slug = slugify_project_path(pane_dir)
    # Every interior separator turned into a dash
    assert slug == "-Users-alice-proj--dialectic-a-04161138-abcd"
    # Chain id still discoverable by inspection (useful for debugging)
    assert "04161138-abcd" in slug
