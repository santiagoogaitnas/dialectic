"""Cross-process integration tests for project_coordinator.

Complements the thread-based concurrency coverage in
test_project_coordinator.py by exercising the module across real OS
processes — which is the actual deployment shape for multi-chain runs.
Thread tests prove in-process re-entrance is safe; these prove the
fcntl locking and on-disk state format survive contention between
truly independent Python processes.

The tests also invoke the module via `python3 -m project_coordinator`,
which goes through the `if __name__ == "__main__"` startup path that
the in-process _cli_main() tests bypass.

Runs in a few seconds — each subprocess operation is short-lived and
capped with explicit timeouts.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.resolve()

# Make project_coordinator importable in this test process too, so we
# can seed/inspect state via the Python API.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import project_coordinator as pc  # noqa: E402


# --- Subprocess helpers ---

def _child_env() -> dict:
    """Environment for child procs so they can `import project_coordinator`."""
    env = {**os.environ}
    existing = env.get("PYTHONPATH", "")
    parts = [str(PROJECT_ROOT)] + ([existing] if existing else [])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def _run_module(project_dir: Path, *args: str, timeout: int = 15):
    """Invoke `python3 -m project_coordinator --project <dir> ...` and return CompletedProcess."""
    cmd = [sys.executable, "-m", "project_coordinator",
           "--project", str(project_dir), *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_child_env(),
    )


def _run_inline(script: str, timeout: int = 15):
    """Invoke `python3 -c '<script>'` with PYTHONPATH set. Returns CompletedProcess."""
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_child_env(),
    )


# Short helper script for the claim-contention tests. Writes one JSON
# result line to stdout so the parent can inspect outcomes. Uses a
# barrier file so both children start racing at the same moment.
_CLAIM_SCRIPT = """
import json, sys, time
from pathlib import Path
import project_coordinator as pc

project = Path(sys.argv[1])
chain_id = sys.argv[2]
files = [f for f in sys.argv[3].split(",") if f]
barrier = Path(sys.argv[4])

deadline = time.time() + 5
while not barrier.exists():
    if time.time() > deadline:
        print(json.dumps({"error": "barrier timeout"}))
        sys.exit(2)
    time.sleep(0.01)

pc.register_chain(project, chain_id)
ok, conflicts = pc.claim_files(project, chain_id, files)
print(json.dumps({"chain_id": chain_id, "ok": ok, "conflicts": conflicts}))
"""


def _spawn_claim(project_dir: Path, chain_id: str, files_csv: str, barrier_path: Path):
    """Launch a claim-contention child process (non-blocking)."""
    return subprocess.Popen(
        [sys.executable, "-c", _CLAIM_SCRIPT,
         str(project_dir), chain_id, files_csv, str(barrier_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_child_env(),
    )


def _wait_json(proc, timeout: int = 10) -> dict:
    """Wait for a claim-contention child and parse its JSON stdout."""
    out, err = proc.communicate(timeout=timeout)
    assert proc.returncode == 0, (
        f"child {proc.pid} failed ({proc.returncode}):\n"
        f"stdout={out!r}\nstderr={err!r}"
    )
    return json.loads(out.strip())


# --- Module invocation (`python3 -m project_coordinator`) ---

def test_module_help_exits_zero_and_mentions_key_flags():
    """`python3 -m project_coordinator --help` must succeed and advertise the CLI surface.

    The in-process _cli_main() tests bypass the `__main__` wrapper; this
    test catches regressions that break module-level invocation (missing
    `__main__` block, argparse misconfiguration, broken imports, etc.)
    """
    result = subprocess.run(
        [sys.executable, "-m", "project_coordinator", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        env=_child_env(),
    )
    assert result.returncode == 0, result.stderr
    for flag in ("--project", "--summary", "--chains", "--claims",
                 "--activity", "--release-stale"):
        assert flag in result.stdout, f"missing {flag} in --help"


def test_module_summary_empty_project(tmp_path):
    """Real subprocess invocation on a clean project prints the empty-state message."""
    result = _run_module(tmp_path, "--summary")
    assert result.returncode == 0, result.stderr
    assert "No active chains" in result.stdout


def test_module_missing_project_exits_1(tmp_path):
    """`--project <does-not-exist>` returns non-zero via the `__main__` path."""
    missing = tmp_path / "does-not-exist"
    result = _run_module(missing, "--summary")
    assert result.returncode == 1
    assert "not found" in result.stdout.lower()


def test_module_requires_a_command(tmp_path):
    """argparse's required mutually-exclusive group makes the module exit non-zero."""
    cmd = [sys.executable, "-m", "project_coordinator",
           "--project", str(tmp_path)]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=10, env=_child_env(),
    )
    assert result.returncode != 0
    # argparse writes the error to stderr.
    assert "one of the arguments" in result.stderr.lower() or \
           "required" in result.stderr.lower()


def test_api_write_visible_to_module_cli(tmp_path):
    """State written via the Python API is observable from `python3 -m project_coordinator`.

    This is the round-trip that matters for the UI and chain.py: they'll
    write via the API, and operators will inspect via the CLI. If the
    on-disk schema doesn't round-trip, the CLI shows a misleading view.
    """
    pc.register_chain(tmp_path, "c1", focus="backend api")
    pc.claim_files(tmp_path, "c1", ["api.py", "db.py"])
    pc.append_note(tmp_path, "c1", "wired router")

    # --summary
    summary = _run_module(tmp_path, "--summary")
    assert summary.returncode == 0, summary.stderr
    assert "c1" in summary.stdout
    assert "backend api" in summary.stdout
    assert "api.py" in summary.stdout

    # --chains: one row per chain, tab-separated
    chains = _run_module(tmp_path, "--chains")
    assert chains.returncode == 0, chains.stderr
    row = next((r for r in chains.stdout.splitlines() if r.startswith("c1\t")), None)
    assert row is not None, f"no c1 row in --chains output: {chains.stdout!r}"

    # --claims: one row per claimed file
    claims = _run_module(tmp_path, "--claims")
    assert claims.returncode == 0, claims.stderr
    assert "api.py\tc1" in claims.stdout
    assert "db.py\tc1" in claims.stdout

    # --activity: the note we appended is present
    activity = _run_module(tmp_path, "--activity")
    assert activity.returncode == 0, activity.stderr
    assert "wired router" in activity.stdout


def test_module_release_stale_drops_aged_chain(tmp_path):
    """`--release-stale` removes chains past the TTL and reports what it dropped."""
    pc.register_chain(tmp_path, "old", focus="went away")
    # Age "old" past the TTL we'll pass on the CLI.
    path = pc.coordination_path(tmp_path)
    data = json.loads(path.read_text())
    data["chains"]["old"]["last_seen"] = time.time() - 99999
    path.write_text(json.dumps(data))

    result = _run_module(tmp_path, "--release-stale", "--ttl", "60")
    assert result.returncode == 0, result.stderr
    assert "old" in result.stdout
    assert pc.get_chain(tmp_path, "old") is None


# --- Cross-process concurrency (real fcntl locking) ---

def test_crossproc_claim_same_file_has_single_winner(tmp_path):
    """Two separate Python processes race for the same file; exactly one wins.

    This is the real shape of multi-chain contention — two chain.py procs
    on the same project both noticing they want the same file at the
    same moment. The thread-based coverage proves the in-process case;
    this proves fcntl survives across process boundaries.
    """
    barrier = tmp_path / "barrier.flag"
    p1 = _spawn_claim(tmp_path, "c1", "contested.py", barrier)
    p2 = _spawn_claim(tmp_path, "c2", "contested.py", barrier)
    try:
        time.sleep(0.15)  # let both procs reach the barrier wait
        barrier.touch()  # fire the race
        r1 = _wait_json(p1)
        r2 = _wait_json(p2)
    finally:
        for p in (p1, p2):
            if p.poll() is None:
                p.kill()

    successes = [r for r in (r1, r2) if r["ok"]]
    failures = [r for r in (r1, r2) if not r["ok"]]
    assert len(successes) == 1, (
        f"expected exactly one winner, got: r1={r1}, r2={r2}"
    )
    assert len(failures) == 1
    assert failures[0]["conflicts"] == ["contested.py"]

    # File-on-disk owner matches the reported winner.
    assert pc.file_owner(tmp_path, "contested.py", ttl=0) == successes[0]["chain_id"]


def test_crossproc_disjoint_claims_both_succeed(tmp_path):
    """Two separate processes claim non-overlapping file sets; both succeed without torn writes."""
    barrier = tmp_path / "barrier.flag"
    p1 = _spawn_claim(tmp_path, "c1", "a.py,b.py,c.py", barrier)
    p2 = _spawn_claim(tmp_path, "c2", "x.py,y.py,z.py", barrier)
    try:
        time.sleep(0.15)
        barrier.touch()
        r1 = _wait_json(p1)
        r2 = _wait_json(p2)
    finally:
        for p in (p1, p2):
            if p.poll() is None:
                p.kill()

    assert r1["ok"] and not r1["conflicts"], r1
    assert r2["ok"] and not r2["conflicts"], r2

    c1 = pc.get_chain(tmp_path, "c1")
    c2 = pc.get_chain(tmp_path, "c2")
    assert set(c1.files_claimed) == {"a.py", "b.py", "c.py"}
    assert set(c2.files_claimed) == {"x.py", "y.py", "z.py"}

    # Activity log contains both claim entries.
    entries = pc.read_activity(tmp_path)
    claim_entries = [e for e in entries if e.kind == "claim"]
    chain_ids = {e.chain_id for e in claim_entries}
    assert chain_ids == {"c1", "c2"}


def test_crossproc_many_disjoint_claims_serialize_cleanly(tmp_path):
    """Four processes each claim a set of distinct files; every claim lands without loss."""
    barrier = tmp_path / "barrier.flag"
    plans = [
        ("p0", "p0-a.py,p0-b.py,p0-c.py"),
        ("p1", "p1-a.py,p1-b.py,p1-c.py"),
        ("p2", "p2-a.py,p2-b.py,p2-c.py"),
        ("p3", "p3-a.py,p3-b.py,p3-c.py"),
    ]
    procs = [_spawn_claim(tmp_path, cid, files, barrier) for cid, files in plans]
    try:
        time.sleep(0.2)
        barrier.touch()
        results = [_wait_json(p) for p in procs]
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()

    for r in results:
        assert r["ok"] and not r["conflicts"], r

    # State file must have all 12 files across 4 chains — no torn write
    # could silently drop one.
    all_claimed = []
    for cid, _ in plans:
        rec = pc.get_chain(tmp_path, cid)
        assert rec is not None, f"{cid} missing from state after race"
        all_claimed.extend(rec.files_claimed)
    assert len(all_claimed) == 12
    assert len(set(all_claimed)) == 12  # no dupes


def test_crossproc_corruption_does_not_wedge_next_process(tmp_path):
    """A half-written/garbled state file shouldn't prevent the next process from operating.

    Write-then-crash is a realistic failure mode; the module's contract
    is that read-side corruption is tolerated and write-side overwrites it.
    """
    # Seed so .dialectic exists.
    pc.register_chain(tmp_path, "seed")

    # Simulate crash mid-write: leave the file truncated + invalid.
    pc.coordination_path(tmp_path).write_text("{partial")

    # A subprocess tries to register — should silently recover.
    script = (
        "import sys; sys.path.insert(0, %r);"
        "import project_coordinator as pc;"
        "rec = pc.register_chain(%r, 'c_recover', focus='after-crash');"
        "print(rec.focus)"
    ) % (str(PROJECT_ROOT), str(tmp_path))
    result = _run_inline(script)
    assert result.returncode == 0, result.stderr
    assert "after-crash" in result.stdout

    # State file is valid JSON again with just the new chain.
    data = json.loads(pc.coordination_path(tmp_path).read_text())
    assert "c_recover" in data["chains"]


def test_crossproc_survives_killed_child_holding_claims(tmp_path):
    """Child proc dies holding claims; parent can still write + release-stale evicts.

    SIGKILL is the extreme case: no finally blocks run. With fcntl the
    OS releases the lock when the fd closes at process exit, so the
    next process is never blocked on the sidecar lock. The state file's
    claim entries for the dead chain remain until `release_stale` or
    a live chain with TTL-based transfer kicks them out.
    """
    # A child that registers, claims, and then sleeps so we can kill it.
    hang_script = """
import sys, time
import project_coordinator as pc
pc.register_chain(sys.argv[1], "doomed", focus="about to die")
pc.claim_files(sys.argv[1], "doomed", ["file-in-limbo.py"])
time.sleep(60)
"""
    p = subprocess.Popen(
        [sys.executable, "-c", hang_script, str(tmp_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_child_env(),
    )
    try:
        # Wait for the child to finish its claim (poll state).
        deadline = time.time() + 5
        while time.time() < deadline:
            rec = pc.get_chain(tmp_path, "doomed")
            if rec and "file-in-limbo.py" in rec.files_claimed:
                break
            time.sleep(0.05)
        else:
            p.kill()
            pytest.fail("child never registered its claim")

        # SIGKILL — no cleanup runs.
        p.kill()
        p.wait(timeout=5)
    finally:
        if p.poll() is None:
            p.kill()

    # Parent can still write — sidecar lock was released at process death.
    pc.register_chain(tmp_path, "survivor")
    pc.append_note(tmp_path, "survivor", "post-crash write")
    entries = pc.read_activity(tmp_path)
    assert any(e.chain_id == "survivor" and "post-crash" in e.summary for e in entries)

    # Manually age "doomed" past the TTL we'll pass in, then release-stale.
    coord = pc.coordination_path(tmp_path)
    data = json.loads(coord.read_text())
    data["chains"]["doomed"]["last_seen"] = time.time() - 99999
    coord.write_text(json.dumps(data))

    removed = pc.release_stale(tmp_path, ttl=60)
    assert "doomed" in removed
    assert pc.file_owner(tmp_path, "file-in-limbo.py", ttl=60) is None
