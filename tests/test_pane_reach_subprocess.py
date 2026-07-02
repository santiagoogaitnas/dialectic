"""Subprocess-level verification that the in-chain coordination protocol
is reachable from a pane's actual working directory.

``chain.py`` writes a CLAUDE.md into each pane's scratch dir
(``<project>/.dialectic-a-<chain_id>/`` or ``-b-``). That CLAUDE.md tells
the spawned ``claude`` agent to run ``project_coordinator`` commands
before every file edit. The text-format assertions in
``test_coordination_prompt.py`` and ``test_claude_md_coordination_wiring.py``
prove the rendered string is well-formed. They do NOT prove the rendered
command actually executes — the pane cwd is nowhere near the dialectic
repo and the spawned ``claude`` has no PYTHONPATH pointing back at it.

These tests close that gap. Each case either:

- spawns ``python3 <abs>/project_coordinator.py --project <tmp> <flag>``
  from inside a fake pane cwd with a scrubbed environment (no
  PYTHONPATH, no repo on sys.path) and asserts exit 0 + expected output,
  OR
- spawns the legacy ``python3 -m project_coordinator ...`` form from the
  same scrubbed cwd and asserts failure — which pins the regression the
  absolute-path fix exists to prevent. If someone reverts the protocol
  body to the ``-m`` form, this test catches it the same way a real
  in-chain agent would catch it (via broken ``Bash`` tool calls).

Design notes
------------

- Uses ``sys.executable`` so the child runs the same interpreter pytest
  is under (avoids macOS system-python mismatches).
- Builds a minimal environment: PATH + HOME + a few shell essentials.
  Explicitly DOES NOT propagate PYTHONPATH from the test process, which
  would mask the bug we're trying to pin.
- Creates a real .dialectic-a-<fake_chain_id>/ directory so the cwd we
  shell from matches the on-disk pane cwd shape.
- Uses short timeouts (10s) so a hung subprocess fails fast.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).parent.parent.resolve()
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import project_coordinator as pc  # noqa: E402


PC_SCRIPT = Path(pc.__file__).resolve()


# --- Helpers ---


def _pane_cwd(tmp_path: Path, chain_id: str = "04180129-fake") -> Path:
    """Build a pane-like scratch dir inside tmp_path.

    Matches the shape chain.py's ``_project_pane_dirs`` creates:
    ``<project>/.dialectic-a-<chain_id>/``. The exact name doesn't
    change behavior — project_coordinator doesn't care about cwd — but
    keeping the shape identical to production makes failures easier to
    diagnose if the fix ever regresses.
    """
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    pane = project / f".dialectic-a-{chain_id}"
    pane.mkdir(exist_ok=True)
    return pane


def _clean_env() -> dict:
    """Minimal child env — no PYTHONPATH, no repo on sys.path.

    Mirrors what the spawned ``claude`` process gets: just the shell
    essentials. If the coordinator command depends on parent PYTHONPATH
    to work, it will break here — which is exactly the bug we want to
    surface.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    # Explicitly do NOT carry PYTHONPATH, VIRTUAL_ENV, PYTHONHOME — the
    # pane's claude doesn't get those either.
    return env


def _run(cwd: Path, args: list[str], timeout: int = 10) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_clean_env(),
    )


# --- The happy path: absolute script path works from any cwd ---


def test_absolute_script_summary_works_from_pane_cwd(tmp_path):
    """The command the rendered CLAUDE.md gives agents must execute
    from the pane cwd with zero PYTHONPATH help.

    This is the protocol's whole contract: agents run the command
    verbatim and it just works.
    """
    pane = _pane_cwd(tmp_path)
    project = tmp_path / "project"

    result = _run(pane, [str(PC_SCRIPT), "--project", str(project), "--summary"])

    assert result.returncode == 0, (
        f"--summary via abs-path failed from pane cwd\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    # Empty project → friendly "no active chains" line.
    assert "No active chains" in result.stdout


def test_absolute_script_chains_works_from_pane_cwd(tmp_path):
    pane = _pane_cwd(tmp_path)
    project = tmp_path / "project"

    result = _run(pane, [str(PC_SCRIPT), "--project", str(project), "--chains"])

    assert result.returncode == 0, (
        f"--chains via abs-path failed from pane cwd\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    assert "no active chains" in result.stdout.lower()


def test_absolute_script_claims_works_from_pane_cwd(tmp_path):
    pane = _pane_cwd(tmp_path)
    project = tmp_path / "project"

    result = _run(pane, [str(PC_SCRIPT), "--project", str(project), "--claims"])

    assert result.returncode == 0
    assert "no claims" in result.stdout.lower()


def test_absolute_script_claim_then_release_roundtrips_from_pane_cwd(tmp_path):
    """Full mutate-side protocol: register via the Python API (which the
    parent chain supervisor does), then claim + release via the CLI
    from the pane cwd (which the in-chain agent does). Both halves of
    the dance must meet on the same coordination.json.
    """
    pane = _pane_cwd(tmp_path, chain_id="04180129-abcd")
    project = tmp_path / "project"

    # Supervisor-side: register the chain. This is done in-process by
    # chain_coordinator.ChainCoordinatorContext, so we mirror that here
    # via the Python API.
    pc.register_chain(project, "04180129-abcd", focus="subproc-test")

    # Agent-side: claim a file via the CLI, from the pane cwd.
    claim = _run(
        pane,
        [
            str(PC_SCRIPT), "--project", str(project),
            "--claim", "src/foo.py", "--chain", "04180129-abcd",
        ],
    )
    assert claim.returncode == 0, (
        f"--claim via abs-path failed\n"
        f"stdout: {claim.stdout!r}\nstderr: {claim.stderr!r}"
    )
    assert "Claimed 1 file" in claim.stdout

    # Verify the parent can see the claim.
    assert pc.file_owner(project, "src/foo.py") == "04180129-abcd"

    # Agent-side: release the file.
    release = _run(
        pane,
        [
            str(PC_SCRIPT), "--project", str(project),
            "--release", "src/foo.py", "--chain", "04180129-abcd",
        ],
    )
    assert release.returncode == 0
    assert "Released 1 file" in release.stdout
    assert pc.file_owner(project, "src/foo.py") is None


def test_absolute_script_claim_conflict_exits_2_from_pane_cwd(tmp_path):
    """A conflict on a live chain's claim must surface as exit 2 even
    when the claim is being attempted from the pane shell. This is the
    signal the CLAUDE.md protocol tells agents to watch for:

        > Exit 2 means another live chain already holds one of the files.
        > Pick different work, note it in your plan file...

    If the in-pane invocation can't produce that exit code, the
    documented agent behavior is unreachable.
    """
    pane = _pane_cwd(tmp_path, chain_id="04180129-later")
    project = tmp_path / "project"

    # Seed: chain-a holds shared.py, chain-b is fresh.
    pc.register_chain(project, "chain-a", focus="owner")
    pc.register_chain(project, "04180129-later", focus="claimant")
    ok, conflicts = pc.claim_files(project, "chain-a", ["shared.py"])
    assert ok and not conflicts

    # Agent-side: chain-b tries to claim the same file via the CLI.
    result = _run(
        pane,
        [
            str(PC_SCRIPT), "--project", str(project),
            "--claim", "shared.py", "--chain", "04180129-later",
        ],
    )
    assert result.returncode == 2, (
        f"expected exit 2 on conflict, got {result.returncode}\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    assert "Conflict" in result.stdout
    assert "shared.py" in result.stdout

    # And chain-a still owns it.
    assert pc.file_owner(project, "shared.py") == "chain-a"


# --- The regression fence: `-m project_coordinator` must fail from the pane ---


def test_module_flag_fails_from_pane_cwd_without_pythonpath(tmp_path):
    """Pin the bug the abs-path fix exists to prevent.

    Running ``python3 -m project_coordinator`` from inside
    ``<project>/.dialectic-a-<id>/`` with no PYTHONPATH should fail
    with ``No module named project_coordinator`` — that's the broken
    invocation the earlier CLAUDE.md wording embedded, and the whole
    reason we switched to the absolute-path form.

    If this ever STARTS passing (e.g. someone globally installs the
    module, or adds it to site-packages), the rendered CLAUDE.md could
    silently regress to the ``-m`` form and still work in test — but
    fail in the user's real pane. This test fails loudly to catch that
    mismatch.
    """
    pane = _pane_cwd(tmp_path)
    project = tmp_path / "project"

    result = _run(
        pane,
        ["-m", "project_coordinator", "--project", str(project), "--summary"],
    )

    assert result.returncode != 0, (
        f"`-m project_coordinator` unexpectedly succeeded from pane cwd. "
        f"If this is because project_coordinator is now on the default "
        f"sys.path, the CLAUDE.md protocol's defense against env drift "
        f"needs revisiting.\nstdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    assert "No module named" in result.stderr or "project_coordinator" in result.stderr


def test_module_flag_fails_with_scrubbed_env_in_repo_root(tmp_path):
    """Even run from REPO_ROOT itself, ``-m project_coordinator`` can fail
    when the venv isn't on PATH and sys.path doesn't carry REPO_ROOT.

    This corroborates the earlier test by removing "cwd is elsewhere"
    as a variable. The bug isn't about cwd per se — it's about
    sys.path not containing REPO_ROOT. The pane cwd just happens to
    be the place this hits in production.
    """
    project = tmp_path / "project"
    project.mkdir()

    # Run from a neutral directory that's neither REPO_ROOT nor a pane.
    result = _run(
        tmp_path,
        ["-m", "project_coordinator", "--project", str(project), "--summary"],
    )
    # Either it fails (expected — scrubbed env has no path to the module),
    # or it happened to pick the module up via a site-packages install,
    # in which case the abs-path form is still safer.
    if result.returncode == 0:
        pytest.skip(
            "project_coordinator is installed on the default Python path; "
            "the `-m` form works here but the abs-path form is still the "
            "correct protocol command because it doesn't depend on that."
        )
    assert "No module named" in result.stderr or "project_coordinator" in result.stderr


# --- Cwd-independence sanity checks ---


def test_absolute_script_works_from_unrelated_cwd(tmp_path):
    """The abs-path form must be truly cwd-independent, not just
    "happens to work from the pane". Any dir should do.
    """
    random_dir = tmp_path / "unrelated-place"
    random_dir.mkdir()
    project = tmp_path / "project"
    project.mkdir()

    result = _run(
        random_dir,
        [str(PC_SCRIPT), "--project", str(project), "--summary"],
    )

    assert result.returncode == 0, (
        f"abs-path form failed from unrelated cwd\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    assert "No active chains" in result.stdout


def test_absolute_script_path_actually_exists():
    """Trivial-looking but important: the abs path we hand to agents
    must resolve to a real file. A CLAUDE.md pointing at a nonexistent
    script is as bad as pointing at ``-m project_coordinator``.
    """
    assert PC_SCRIPT.is_file(), (
        f"project_coordinator.py not found at {PC_SCRIPT}"
    )
    assert PC_SCRIPT.is_absolute()
    assert PC_SCRIPT.name == "project_coordinator.py"
