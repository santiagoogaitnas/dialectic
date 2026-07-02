"""Chain registry — tracks active and completed chains.

Stores chain metadata in a JSON file under chainwork/.registry.json.
Provides locking via fcntl to handle concurrent reads/writes from
multiple chain processes and the UI server.
"""

import fcntl
import json
import logging
import os
import signal
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

logger = logging.getLogger("registry")

REPO_DIR = Path(__file__).parent.resolve()
WORKSPACE = REPO_DIR / "chainwork"
REGISTRY_FILE = WORKSPACE / ".registry.json"


CLEAR_EVERY_DEFAULT = 5


@dataclass
class ChainConfig:
    """All configuration for a single chain run."""

    chain_id: str
    session: str
    seed: str = ""
    role_a: str = "builder.txt"
    role_b: str = "thinker.txt"
    project: Optional[str] = None
    clear_every: int = CLEAR_EVERY_DEFAULT

    @property
    def workspace(self) -> Path:
        return WORKSPACE / self.chain_id

    @property
    def ws_a(self) -> Path:
        return self.workspace / "a"

    @property
    def ws_b(self) -> Path:
        return self.workspace / "b"

    @property
    def log_file(self) -> Path:
        return self.workspace / "chain_log.md"

    @property
    def bulletin_path(self) -> Path:
        return self.workspace / "bulletin.md"

    @property
    def project_dir(self) -> Optional[Path]:
        if self.project:
            return Path(self.project)
        return None

    @property
    def plan_path(self) -> Optional[Path]:
        """Per-chain plan file inside the project, isolated from other chains."""
        pdir = self.project_dir
        if pdir is None:
            return None
        return pdir / f"plan-{self.chain_id}.md"

    @property
    def pane_a(self) -> str:
        return f"{self.session}:0.0"

    @property
    def pane_b(self) -> str:
        return f"{self.session}:0.1"

    def pane_dirs(self) -> tuple[Path, Path]:
        """Per-pane cwd directories. In project mode, chain-specific subdirs."""
        if self.project_dir:
            dir_a = self.project_dir / f".dialectic-a-{self.chain_id}"
            dir_b = self.project_dir / f".dialectic-b-{self.chain_id}"
            dir_a.mkdir(exist_ok=True)
            dir_b.mkdir(exist_ok=True)
            return dir_a, dir_b
        return self.ws_a, self.ws_b


@dataclass
class ChainRecord:
    """Metadata stored in the registry for one chain."""

    chain_id: str
    session: str
    seed: str
    role_a: str
    role_b: str
    project: Optional[str]
    pid: int
    status: str = "running"  # running, stopped, dead
    started_at: float = 0.0
    stopped_at: Optional[float] = None
    current_round: int = 0
    last_activity: float = 0.0
    last_output_snippet: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ChainRecord":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    @classmethod
    def from_config(cls, config: ChainConfig, pid: int) -> "ChainRecord":
        now = time.time()
        return cls(
            chain_id=config.chain_id,
            session=config.session,
            seed=config.seed,
            role_a=config.role_a,
            role_b=config.role_b,
            project=config.project,
            pid=pid,
            started_at=now,
            last_activity=now,
        )


def generate_chain_id() -> str:
    """Short unique chain ID: timestamp prefix + random suffix."""
    ts = time.strftime("%m%d%H%M")
    suffix = uuid.uuid4().hex[:4]
    return f"{ts}-{suffix}"


def _read_registry() -> dict:
    """Read the registry file. Returns empty dict if missing/corrupt."""
    if not REGISTRY_FILE.exists():
        return {"chains": {}}
    try:
        data = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
        if "chains" not in data:
            data["chains"] = {}
        return data
    except (json.JSONDecodeError, OSError):
        return {"chains": {}}


def _write_registry(data: dict) -> None:
    """Write registry atomically."""
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.rename(REGISTRY_FILE)


def _with_lock(func):
    """Decorator for registry operations that need file locking."""
    def wrapper(*args, **kwargs):
        WORKSPACE.mkdir(parents=True, exist_ok=True)
        lock_path = REGISTRY_FILE.with_suffix(".lock")
        with open(lock_path, "w") as lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                return func(*args, **kwargs)
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
    return wrapper


@_with_lock
def register_chain(config: ChainConfig, pid: int) -> ChainRecord:
    """Register a new chain in the registry."""
    data = _read_registry()
    record = ChainRecord.from_config(config, pid)
    data["chains"][config.chain_id] = record.to_dict()
    _write_registry(data)
    logger.info(f"Registered chain {config.chain_id} (pid={pid})")
    return record


@_with_lock
def unregister_chain(chain_id: str) -> None:
    """Mark a chain as stopped."""
    data = _read_registry()
    if chain_id in data["chains"]:
        data["chains"][chain_id]["status"] = "stopped"
        data["chains"][chain_id]["stopped_at"] = time.time()
        _write_registry(data)


@_with_lock
def update_chain(chain_id: str, **kwargs) -> None:
    """Update fields on a chain record."""
    data = _read_registry()
    if chain_id in data["chains"]:
        data["chains"][chain_id].update(kwargs)
        data["chains"][chain_id]["last_activity"] = time.time()
        _write_registry(data)


def list_chains(active_only: bool = False) -> list[ChainRecord]:
    """List all registered chains."""
    data = _read_registry()
    records = []
    for d in data["chains"].values():
        record = ChainRecord.from_dict(d)
        # Check if process is still alive
        if record.status == "running":
            try:
                os.kill(record.pid, 0)
            except (OSError, ProcessLookupError):
                record.status = "dead"
        if active_only and record.status not in ("running",):
            continue
        records.append(record)
    return records


def get_chain(chain_id: str) -> Optional[ChainRecord]:
    """Get a single chain record."""
    data = _read_registry()
    d = data["chains"].get(chain_id)
    if not d:
        return None
    record = ChainRecord.from_dict(d)
    if record.status == "running":
        try:
            os.kill(record.pid, 0)
        except (OSError, ProcessLookupError):
            record.status = "dead"
    return record


def stop_chain(chain_id: str) -> bool:
    """Stop a chain by killing its process and tmux session."""
    record = get_chain(chain_id)
    if not record:
        logger.error(f"Chain {chain_id} not found")
        return False

    if record.status == "running":
        try:
            os.kill(record.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass

    # Kill the tmux session
    import subprocess
    subprocess.run(
        ["tmux", "kill-session", "-t", record.session],
        capture_output=True, check=False,
    )

    unregister_chain(chain_id)
    logger.info(f"Stopped chain {chain_id}")
    return True


def count_active_chains(project: Optional[str] = None) -> int:
    """Count currently running chains, optionally filtered by project."""
    chains = list_chains(active_only=True)
    if project:
        return sum(1 for c in chains if c.project == project)
    return len(chains)


def read_plan_text(chain_id: str) -> Optional[str]:
    """Read the chain's per-chain plan file (plan-<chain_id>.md) from the project.

    Returns the file contents, or None if no project or plan not yet written.
    """
    record = get_chain(chain_id)
    if not record or not record.project:
        return None
    plan_file = Path(record.project) / f"plan-{chain_id}.md"
    if not plan_file.exists():
        return None
    try:
        return plan_file.read_text(encoding="utf-8")
    except OSError:
        return None


@_with_lock
def cleanup_dead_chains() -> int:
    """Mark chains with dead processes as 'dead'. Returns count cleaned."""
    data = _read_registry()
    cleaned = 0
    for chain_id, d in data["chains"].items():
        if d.get("status") == "running":
            try:
                os.kill(d["pid"], 0)
            except (OSError, ProcessLookupError):
                d["status"] = "dead"
                d["stopped_at"] = time.time()
                cleaned += 1
    if cleaned:
        _write_registry(data)
    return cleaned


@_with_lock
def remove_chain(chain_id: str) -> bool:
    """Physically remove a chain record from the registry.

    Unlike unregister_chain(), which only flips status to 'stopped' and keeps
    the row, this deletes the row outright. Use this after a chain is well
    and truly done — the process is gone, nobody needs the log entry anymore.
    Returns True when something was removed, False when the id wasn't present.
    """
    data = _read_registry()
    if chain_id not in data["chains"]:
        return False
    del data["chains"][chain_id]
    _write_registry(data)
    logger.info(f"Removed chain {chain_id} from registry")
    return True


@_with_lock
def prune_inactive(
    statuses: tuple[str, ...] = ("stopped", "dead"),
    older_than_seconds: Optional[float] = None,
) -> list[str]:
    """Remove records matching any of `statuses`. Returns removed chain_ids.

    Before matching, records with status='running' but a dead PID are
    re-classified to 'dead' (same liveness check as cleanup_dead_chains) so
    stale 'running' rows don't get stuck past a prune.

    The `older_than_seconds` guard protects against racing with a chain that
    just stopped milliseconds ago: a record is only eligible if its
    stopped_at is at least `older_than_seconds` in the past. Running chains
    never match regardless. If `older_than_seconds` is None, no time guard.
    """
    data = _read_registry()
    removed: list[str] = []
    now = time.time()

    # Re-classify stale 'running' rows so callers can prune orphans in one pass.
    for d in data["chains"].values():
        if d.get("status") == "running":
            try:
                os.kill(d["pid"], 0)
            except (OSError, ProcessLookupError):
                d["status"] = "dead"
                d["stopped_at"] = now

    eligible_statuses = set(statuses)
    for chain_id, d in list(data["chains"].items()):
        if d.get("status") not in eligible_statuses:
            continue
        if older_than_seconds is not None:
            stopped_at = d.get("stopped_at") or 0
            if not stopped_at or (now - stopped_at) < older_than_seconds:
                continue
        del data["chains"][chain_id]
        removed.append(chain_id)

    if removed or any(d.get("status") == "dead" for d in data["chains"].values()):
        _write_registry(data)
    if removed:
        logger.info(f"Pruned {len(removed)} chain(s): {', '.join(removed)}")
    return removed


def _cli_main(argv: Optional[list[str]] = None) -> int:
    """`python3 -m registry` entry point: list / prune / rm chain records.

    Separate from `python3 chain.py --list/--stop` so that the registry is
    independently manageable — useful for cleaning up stopped/dead chains
    from a shell script or cron without going through chain.py's full argv.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="python3 -m registry",
        description="Inspect and clean up the Dialectic chain registry.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true",
                       help="List every record in the registry with its status.")
    group.add_argument("--prune", action="store_true",
                       help="Remove stopped and dead records.")
    group.add_argument("--rm", metavar="CHAIN_ID",
                       help="Remove a single chain record by id.")
    parser.add_argument("--older-than", type=float, default=None,
                        help="With --prune, only remove records whose "
                             "stopped_at is older than this many seconds.")
    args = parser.parse_args(argv)

    if args.list:
        cleanup_dead_chains()
        records = list_chains()
        if not records:
            print("Registry is empty.")
            return 0
        print(f"{'ID':<16} {'Status':<10} {'PID':<8} {'Session':<16} {'Project':<40}")
        print("-" * 90)
        for r in records:
            project = r.project or "-"
            if len(project) > 40:
                project = "..." + project[-37:]
            print(f"{r.chain_id:<16} {r.status:<10} {r.pid:<8} {r.session:<16} {project:<40}")
        return 0

    if args.prune:
        removed = prune_inactive(older_than_seconds=args.older_than)
        if removed:
            print(f"Removed {len(removed)} record(s): {', '.join(removed)}")
        else:
            print("Nothing to prune.")
        return 0

    if args.rm:
        if remove_chain(args.rm):
            print(f"Removed chain {args.rm}")
            return 0
        print(f"Chain {args.rm} not found")
        return 1

    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    raise SystemExit(_cli_main())
