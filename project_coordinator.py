"""Project-level coordination for multiple chain pairs on one project.

When N chains run on the same project, each pair (Builder + Thinker) needs to
know what the others are doing so they don't collide. This module owns a
single shared file per project:

    <project>/.dialectic/coordination.json

Every chain pair writes into it via this API. Reads are cheap. Writes are
serialized via fcntl so concurrent multi-chain runs are safe.

What it tracks
--------------

- **Presence**: which chains are alive on this project, plus a free-text
  *focus* describing what the pair is working on ("backend api", "tests
  for auth", ...). Presence has a TTL: a pair must heartbeat or it ages
  out, so a dead chain doesn't keep its focus or claims forever.

- **File claims**: which files a chain currently owns. The intent is
  cooperative — agents are expected to consult `file_owner()` before
  editing, and `claim_files()` returns conflicts rather than enforcing
  exclusion. We can't actually stop a misbehaving agent from writing.

- **Activity log**: append-only record of focus changes, claims, and
  notes. Bounded length (oldest entries dropped) so the file doesn't
  grow without bound.

What it does NOT do
-------------------

- Doesn't talk to the chain registry (`registry.py`). That's a process /
  tmux registry; this is a project-scoped coordination file. They serve
  different concerns and live at different paths on purpose.
- Doesn't run subprocesses, talk to git, or modify project files. It's
  pure JSON state with locking.
- Doesn't decide policy (who wins a claim conflict, when to release).
  Callers do that. This module just exposes the primitives.

CLI
---

    python3 -m project_coordinator --project /path --summary
    python3 -m project_coordinator --project /path --chains
    python3 -m project_coordinator --project /path --claims
    python3 -m project_coordinator --project /path --activity
    python3 -m project_coordinator --project /path --claim-rate [--window 3600]
    python3 -m project_coordinator --project /path --release-stale
    python3 -m project_coordinator --project /path --claim file.py --chain ID
    python3 -m project_coordinator --project /path --release file.py --chain ID
    python3 -m project_coordinator --project /path --release --chain ID

The summary text is what an agent or the UI can quote verbatim to show
what's going on across all chains on the project. --claim / --release
are the write-side mutators agents invoke from the pane shell before and
after edits; they require --chain and a live registration (which
``chain_coordinator.ChainCoordinatorContext`` handles in the parent
chain process).
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

logger = logging.getLogger("project_coordinator")

COORD_DIRNAME = ".dialectic"
COORD_FILENAME = "coordination.json"

PRESENCE_TTL_DEFAULT = 600.0  # 10 minutes; tune via heartbeat() cadence
ACTIVITY_LOG_MAX = 200        # Keep the most recent N entries

SCHEMA_VERSION = 1


@dataclass
class ChainPresence:
    """One chain pair's footprint on a project."""

    chain_id: str
    focus: str = ""
    files_claimed: list[str] = field(default_factory=list)
    started_at: float = 0.0
    last_seen: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ChainPresence":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class ActivityEntry:
    """One row in the activity log."""

    chain_id: str
    timestamp: float
    kind: str       # 'register', 'deregister', 'focus', 'claim', 'release', 'note'
    summary: str
    files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ActivityEntry":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# --- Paths ---

def coordination_dir(project_dir: Path) -> Path:
    """The .dialectic directory inside a project."""
    return Path(project_dir) / COORD_DIRNAME


def coordination_path(project_dir: Path) -> Path:
    """Full path to the coordination JSON file."""
    return coordination_dir(project_dir) / COORD_FILENAME


# --- Locking + IO ---

def _empty_state() -> dict:
    return {"version": SCHEMA_VERSION, "chains": {}, "activity": []}


def _read_state(project_dir: Path) -> dict:
    """Read state, returning a fresh empty doc if missing or corrupted.

    Corruption recovery is intentional: a half-written file from a crash
    shouldn't strand every other chain on the project. The lost data is
    presence + activity, both of which are continually re-asserted.
    """
    path = coordination_path(project_dir)
    if not path.exists():
        return _empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning(f"Coordination file unreadable, resetting: {path}")
        return _empty_state()
    # Migrate forward if we ever bump SCHEMA_VERSION; for now just shape-check.
    if not isinstance(data, dict):
        return _empty_state()
    data.setdefault("version", SCHEMA_VERSION)
    data.setdefault("chains", {})
    data.setdefault("activity", [])
    return data


def _write_state(project_dir: Path, data: dict) -> None:
    """Atomic write via tmp file + rename."""
    cdir = coordination_dir(project_dir)
    cdir.mkdir(parents=True, exist_ok=True)
    path = coordination_path(project_dir)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.rename(path)


def _with_lock(func):
    """Serialize state mutations across processes via fcntl on a sidecar lock."""
    def wrapper(project_dir, *args, **kwargs):
        cdir = coordination_dir(project_dir)
        cdir.mkdir(parents=True, exist_ok=True)
        lock_path = cdir / (COORD_FILENAME + ".lock")
        with open(lock_path, "w") as lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                return func(project_dir, *args, **kwargs)
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
    return wrapper


def _append_activity(data: dict, entry: ActivityEntry) -> None:
    """In-place append with bounded length."""
    log = data.setdefault("activity", [])
    log.append(entry.to_dict())
    if len(log) > ACTIVITY_LOG_MAX:
        del log[: len(log) - ACTIVITY_LOG_MAX]


# --- Presence ---

@_with_lock
def register_chain(
    project_dir: Path, chain_id: str, focus: str = "",
) -> ChainPresence:
    """Add (or refresh) a chain's presence on the project.

    Re-registering an existing chain_id updates last_seen and focus but
    preserves started_at and files_claimed — useful when a chain process
    restarts and wants to reclaim its slot without dropping in-flight claims.
    """
    if not chain_id:
        raise ValueError("chain_id is required")
    data = _read_state(project_dir)
    now = time.time()
    chains = data["chains"]
    if chain_id in chains:
        record = ChainPresence.from_dict(chains[chain_id])
        record.last_seen = now
        if focus:
            record.focus = focus
    else:
        record = ChainPresence(
            chain_id=chain_id,
            focus=focus,
            files_claimed=[],
            started_at=now,
            last_seen=now,
        )
    chains[chain_id] = record.to_dict()
    _append_activity(
        data,
        ActivityEntry(
            chain_id=chain_id, timestamp=now, kind="register", summary=focus,
        ),
    )
    _write_state(project_dir, data)
    logger.info(f"Registered chain {chain_id} on {project_dir} (focus={focus!r})")
    return record


@_with_lock
def heartbeat(project_dir: Path, chain_id: str) -> bool:
    """Bump last_seen for a chain. Returns False if the chain isn't registered."""
    data = _read_state(project_dir)
    if chain_id not in data["chains"]:
        return False
    data["chains"][chain_id]["last_seen"] = time.time()
    _write_state(project_dir, data)
    return True


@_with_lock
def set_focus(project_dir: Path, chain_id: str, focus: str) -> bool:
    """Update the focus string for a chain. Returns False if not registered.

    Records a 'focus' activity entry so other chains looking at the log can
    see what shifted.
    """
    data = _read_state(project_dir)
    if chain_id not in data["chains"]:
        return False
    data["chains"][chain_id]["focus"] = focus
    data["chains"][chain_id]["last_seen"] = time.time()
    _append_activity(
        data,
        ActivityEntry(
            chain_id=chain_id, timestamp=time.time(),
            kind="focus", summary=focus,
        ),
    )
    _write_state(project_dir, data)
    return True


@_with_lock
def deregister_chain(project_dir: Path, chain_id: str) -> bool:
    """Remove a chain's presence entirely. Returns False if not present."""
    data = _read_state(project_dir)
    if chain_id not in data["chains"]:
        return False
    del data["chains"][chain_id]
    _append_activity(
        data,
        ActivityEntry(
            chain_id=chain_id, timestamp=time.time(),
            kind="deregister", summary="",
        ),
    )
    _write_state(project_dir, data)
    logger.info(f"Deregistered chain {chain_id} on {project_dir}")
    return True


def list_chains(
    project_dir: Path, ttl: float = PRESENCE_TTL_DEFAULT,
) -> list[ChainPresence]:
    """Return chains whose last_seen is within `ttl` seconds.

    Pass ttl=0 (or negative) to return everything regardless of age.
    Read-only; does not prune the file. Use `release_stale()` for that.
    """
    data = _read_state(project_dir)
    now = time.time()
    out: list[ChainPresence] = []
    for d in data["chains"].values():
        rec = ChainPresence.from_dict(d)
        if ttl > 0 and (now - rec.last_seen) > ttl:
            continue
        out.append(rec)
    out.sort(key=lambda r: r.started_at)
    return out


def get_chain(project_dir: Path, chain_id: str) -> Optional[ChainPresence]:
    """Look up a single chain's presence by id."""
    data = _read_state(project_dir)
    d = data["chains"].get(chain_id)
    return ChainPresence.from_dict(d) if d else None


# --- File claims ---

def _normalize_files(files) -> list[str]:
    """Coerce input to a deduplicated, str-typed list. Path-like accepted."""
    out: list[str] = []
    seen: set[str] = set()
    for f in files:
        s = str(f)
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


@_with_lock
def claim_files(
    project_dir: Path, chain_id: str, files,
    ttl: float = PRESENCE_TTL_DEFAULT,
) -> tuple[bool, list[str]]:
    """Try to claim a set of files for a chain.

    A file held by a *live* (within ttl) other chain blocks the claim and is
    returned in the conflicts list. A file held by a stale chain is silently
    transferred — stale presence shouldn't keep the project gridlocked. The
    chain itself must be registered first.

    On success: every requested file is added to this chain's claim set
    (idempotent — re-claiming files you already own is a no-op) and the
    function returns (True, []).
    On conflict: nothing is added; returns (False, [conflicting_files...])
    so the caller can decide whether to wait, pick different files, or pass
    an explicit force flag in a future caller.
    """
    files = _normalize_files(files)
    data = _read_state(project_dir)
    if chain_id not in data["chains"]:
        raise ValueError(f"chain {chain_id} not registered on {project_dir}")
    now = time.time()
    chains = data["chains"]

    conflicts: list[str] = []
    for f in files:
        for other_id, other in chains.items():
            if other_id == chain_id:
                continue
            if f not in other.get("files_claimed", []):
                continue
            other_age = now - other.get("last_seen", 0)
            if ttl > 0 and other_age > ttl:
                # Stale — drop their claim silently.
                other["files_claimed"] = [
                    x for x in other["files_claimed"] if x != f
                ]
                continue
            conflicts.append(f)
            break

    if conflicts:
        return False, conflicts

    held = set(chains[chain_id].get("files_claimed", []))
    for f in files:
        held.add(f)
    chains[chain_id]["files_claimed"] = sorted(held)
    chains[chain_id]["last_seen"] = now
    _append_activity(
        data,
        ActivityEntry(
            chain_id=chain_id, timestamp=now,
            kind="claim", summary=f"{len(files)} file(s)", files=files,
        ),
    )
    _write_state(project_dir, data)
    return True, []


@_with_lock
def release_files(
    project_dir: Path, chain_id: str, files=None,
) -> int:
    """Release `files` (or all files this chain holds when files is None).

    Returns the number of files actually released (i.e. that were claimed).
    Raises ValueError if the chain isn't registered, so callers don't
    silently lose track of stale state.
    """
    data = _read_state(project_dir)
    if chain_id not in data["chains"]:
        raise ValueError(f"chain {chain_id} not registered on {project_dir}")
    held = list(data["chains"][chain_id].get("files_claimed", []))
    if not held:
        return 0
    if files is None:
        to_release = held
    else:
        wanted = set(_normalize_files(files))
        to_release = [f for f in held if f in wanted]
    if not to_release:
        return 0
    remaining = [f for f in held if f not in to_release]
    data["chains"][chain_id]["files_claimed"] = remaining
    data["chains"][chain_id]["last_seen"] = time.time()
    _append_activity(
        data,
        ActivityEntry(
            chain_id=chain_id, timestamp=time.time(),
            kind="release", summary=f"{len(to_release)} file(s)",
            files=to_release,
        ),
    )
    _write_state(project_dir, data)
    return len(to_release)


def file_owner(
    project_dir: Path, file_path, ttl: float = PRESENCE_TTL_DEFAULT,
) -> Optional[str]:
    """Return chain_id holding `file_path`, or None.

    Stale claims (presence older than ttl) are ignored — they're effectively
    "no owner" from the caller's perspective. Pass ttl=0 to ignore staleness.
    """
    target = str(file_path)
    data = _read_state(project_dir)
    now = time.time()
    for chain_id, rec in data["chains"].items():
        if target not in rec.get("files_claimed", []):
            continue
        age = now - rec.get("last_seen", 0)
        if ttl > 0 and age > ttl:
            continue
        return chain_id
    return None


# --- Bulk maintenance ---

@_with_lock
def release_stale(
    project_dir: Path, ttl: float = PRESENCE_TTL_DEFAULT,
) -> list[str]:
    """Drop every chain whose presence is older than ttl. Returns chain_ids removed.

    Activity log entries are NOT removed (those are append-only history).
    """
    data = _read_state(project_dir)
    now = time.time()
    removed: list[str] = []
    for chain_id in list(data["chains"].keys()):
        rec = data["chains"][chain_id]
        if (now - rec.get("last_seen", 0)) > ttl:
            del data["chains"][chain_id]
            removed.append(chain_id)
            _append_activity(
                data,
                ActivityEntry(
                    chain_id=chain_id, timestamp=now,
                    kind="deregister", summary="released-stale",
                ),
            )
    if removed:
        _write_state(project_dir, data)
    return removed


# --- Activity log ---

@_with_lock
def append_note(
    project_dir: Path, chain_id: str, summary: str, files=None,
) -> None:
    """Free-form activity entry — for chains to record completions or signals.

    Doesn't validate that the chain is registered — notes from a deregistered
    chain ('I finished my focus area') are still useful for the audit trail.
    """
    data = _read_state(project_dir)
    _append_activity(
        data,
        ActivityEntry(
            chain_id=chain_id, timestamp=time.time(),
            kind="note", summary=summary,
            files=_normalize_files(files) if files else [],
        ),
    )
    _write_state(project_dir, data)


def read_activity(
    project_dir: Path, since: Optional[float] = None, limit: int = 100,
) -> list[ActivityEntry]:
    """Read the most recent `limit` entries, newest last. Optional since-filter."""
    data = _read_state(project_dir)
    entries = [ActivityEntry.from_dict(e) for e in data.get("activity", [])]
    if since is not None:
        entries = [e for e in entries if e.timestamp >= since]
    if limit > 0:
        entries = entries[-limit:]
    return entries


def claim_rate(
    project_dir: Path, window_seconds: float = 3600.0,
) -> dict[str, int]:
    """Count 'claim' activity entries per chain within a rolling time window.

    Observability for whether in-chain agents actually run the coordination
    protocol's ``--claim`` CLI (which the CLAUDE.md addendum tells them
    they MUST run before every edit). A live chain with zero claims over a
    long window on a project where edits are happening is a signal the
    protocol is being ignored.

    Returns ``{chain_id: count}`` for chains with at least one ``claim``
    activity entry in the last ``window_seconds``. ``register``,
    ``deregister``, ``focus``, ``release``, and ``note`` entries are
    ignored — this is specifically a claim-activity metric. Chains with
    no claims in the window are omitted from the result.

    Read-only; does not take the coordination write lock.
    """
    if window_seconds <= 0:
        return {}
    cutoff = time.time() - window_seconds
    counts: dict[str, int] = {}
    for entry in read_activity(
        project_dir, since=cutoff, limit=ACTIVITY_LOG_MAX,
    ):
        if entry.kind != "claim":
            continue
        counts[entry.chain_id] = counts.get(entry.chain_id, 0) + 1
    return counts


# --- Human-readable summary ---

def _format_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def render_summary(
    project_dir: Path, ttl: float = PRESENCE_TTL_DEFAULT,
) -> str:
    """Produce a compact text snapshot agents/UI can quote verbatim.

    Lists active chains, their focus, what files they're holding, and the
    last few activity entries. Empty-state is intentional: returns a one-
    liner so agents handed an empty project don't get a wall of headers.
    """
    chains = list_chains(project_dir, ttl=ttl)
    if not chains:
        return f"No active chains on {project_dir} (within ttl={int(ttl)}s)."

    now = time.time()
    lines = [f"Active chains on {project_dir}:"]
    for c in chains:
        age = _format_age(now - c.last_seen)
        focus = c.focus or "(no focus set)"
        lines.append(f"  - {c.chain_id}: {focus} (last seen {age})")
        if c.files_claimed:
            lines.append(f"      claims: {', '.join(c.files_claimed)}")

    activity = read_activity(project_dir, limit=5)
    if activity:
        lines.append("")
        lines.append("Recent activity:")
        for e in activity:
            age = _format_age(now - e.timestamp)
            file_hint = f" [{', '.join(e.files)}]" if e.files else ""
            lines.append(
                f"  - {age}  {e.chain_id} {e.kind}: {e.summary}{file_hint}"
            )
    return "\n".join(lines)


# --- CLI ---

def _cli_main(argv: Optional[list[str]] = None) -> int:
    """`python3 -m project_coordinator` entry point.

    Read-only inspection (--summary/--chains/--claims/--activity), bulk
    maintenance (--release-stale), and cooperative mutators for in-chain
    agents (--claim / --release). The mutators require --chain and a live
    registration; callers run these as ordinary shell commands inside their
    pane, e.g. ``python3 -m project_coordinator --project /abs --claim a.py
    --chain CHAIN_ID``.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="python3 -m project_coordinator",
        description="Inspect or maintain a project's coordination state.",
    )
    parser.add_argument(
        "--project", required=True, type=Path,
        help="Project directory containing .dialectic/coordination.json.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--summary", action="store_true",
                       help="Print render_summary().")
    group.add_argument("--chains", action="store_true",
                       help="Print one row per chain (id, focus, claims).")
    group.add_argument("--claims", action="store_true",
                       help="Print one row per file claim.")
    group.add_argument("--activity", action="store_true",
                       help="Print the most recent activity entries.")
    group.add_argument("--claim-rate", action="store_true",
                       help="Print per-chain 'claim' activity counts over "
                            "--window seconds (default 3600). Useful for "
                            "checking whether in-chain agents are following "
                            "the claim-before-edit protocol.")
    group.add_argument("--release-stale", action="store_true",
                       help="Drop chains whose last_seen exceeds the TTL.")
    group.add_argument(
        "--claim", nargs="+", metavar="FILE",
        help="Claim one or more files for --chain. Prints conflicts (if any) "
             "to stdout and exits 2; exits 0 on success.",
    )
    group.add_argument(
        "--release", nargs="*", metavar="FILE",
        help="Release files held by --chain. Pass no files to release every "
             "file the chain currently holds.",
    )
    parser.add_argument("--chain", metavar="CHAIN_ID", default=None,
                        help="Chain id for --claim / --release. Required with "
                             "those flags.")
    parser.add_argument("--ttl", type=float, default=PRESENCE_TTL_DEFAULT,
                        help=f"Liveness window in seconds "
                             f"(default: {int(PRESENCE_TTL_DEFAULT)}).")
    parser.add_argument("--limit", type=int, default=20,
                        help="Max activity rows to print (default: 20).")
    parser.add_argument("--window", type=float, default=3600.0,
                        help="Rolling window (seconds) for --claim-rate "
                             "(default: 3600 = 1 hour).")

    args = parser.parse_args(argv)
    project_dir = args.project

    if not project_dir.exists():
        print(f"Project directory not found: {project_dir}")
        return 1

    if args.summary:
        print(render_summary(project_dir, ttl=args.ttl))
        return 0

    if args.chains:
        chains = list_chains(project_dir, ttl=args.ttl)
        if not chains:
            print("(no active chains)")
            return 0
        for c in chains:
            claims = ", ".join(c.files_claimed) if c.files_claimed else "-"
            focus = c.focus or "-"
            print(f"{c.chain_id}\t{focus}\t{claims}")
        return 0

    if args.claims:
        chains = list_chains(project_dir, ttl=args.ttl)
        any_claim = False
        for c in chains:
            for f in c.files_claimed:
                print(f"{f}\t{c.chain_id}")
                any_claim = True
        if not any_claim:
            print("(no claims)")
        return 0

    if args.activity:
        entries = read_activity(project_dir, limit=args.limit)
        if not entries:
            print("(no activity)")
            return 0
        for e in entries:
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e.timestamp))
            files = f" [{', '.join(e.files)}]" if e.files else ""
            print(f"{ts}\t{e.chain_id}\t{e.kind}\t{e.summary}{files}")
        return 0

    if args.claim_rate:
        rates = claim_rate(project_dir, window_seconds=args.window)
        if not rates:
            print(f"(no claim activity in the last {int(args.window)}s)")
            return 0
        window_hours = args.window / 3600.0
        for chain_id in sorted(rates):
            count = rates[chain_id]
            per_hour = count / window_hours if window_hours > 0 else 0.0
            print(f"{chain_id}\t{count}\t{per_hour:.1f}/hr")
        return 0

    if args.release_stale:
        removed = release_stale(project_dir, ttl=args.ttl)
        if removed:
            print(f"Released {len(removed)} stale chain(s): {', '.join(removed)}")
        else:
            print("Nothing stale.")
        return 0

    if args.claim is not None:
        if not args.chain:
            print("--chain CHAIN_ID is required with --claim.")
            return 1
        try:
            ok, conflicts = claim_files(
                project_dir, args.chain, args.claim, ttl=args.ttl,
            )
        except ValueError as e:
            print(str(e))
            return 1
        if ok:
            print(f"Claimed {len(args.claim)} file(s) for {args.chain}.")
            return 0
        print(
            f"Conflict: {len(conflicts)} file(s) held by another chain: "
            f"{', '.join(conflicts)}"
        )
        return 2

    if args.release is not None:
        if not args.chain:
            print("--chain CHAIN_ID is required with --release.")
            return 1
        try:
            n = release_files(
                project_dir, args.chain, args.release if args.release else None,
            )
        except ValueError as e:
            print(str(e))
            return 1
        print(f"Released {n} file(s) for {args.chain}.")
        return 0

    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    raise SystemExit(_cli_main())
