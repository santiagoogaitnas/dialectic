"""Project-centric views built from the two coordination sources of truth.

Two separate files describe what's running where:

- `chainwork/.registry.json` (via `registry.py`): one row per chain
  *process* — who owns the tmux session, PID, status, round counter.
- `<project>/.dialectic/coordination.json` (via `project_coordinator.py`):
  one row per chain pair's *project presence* — focus area, files claimed,
  heartbeat timestamp.

The dashboard needs a single blended answer: "for each project directory,
which chains are running on it, what are each chain's focus areas, and
which files are currently claimed?" That blend lives here rather than in
`ui/server.py` so the HTTP layer stays a thin adapter on top of library
calls that are easy to unit-test in isolation.

Public API
----------

- `list_projects_with_chains()` — one entry per project with a list of chain
  summaries. Drives the grouped dashboard view.
- `get_project_detail(project_dir)` — everything about one project: chains,
  claims, recent activity. Drives a per-project detail page (future wiring).
- `list_project_conflicts(project_dir)` — files claimed by 2+ live chains on
  the same project. Drives the red "conflict" badge in the UI.
- `project_summary_text(project_dir)` — plain text snapshot agents can quote.
  Thin wrapper over `project_coordinator.render_summary` so callers don't
  need to import two modules.

This module is read-only: it never registers chains, claims files, or
modifies any file on disk. Those mutations stay in `project_coordinator`.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

# Make sibling top-level modules importable regardless of how this package is
# imported (via `python -m ui.server`, pytest, etc.).
_REPO_ROOT = Path(__file__).parent.parent.resolve()
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import project_coordinator as pc
import registry as reg

NO_PROJECT_KEY = "(no project)"

# How far back to look when counting per-chain "claim" activity. One hour
# matches what `python3 -m project_coordinator --claim-rate` defaults to.
# Exposed as a module constant so callers / tests can swap it without
# plumbing a new keyword through every public function.
CLAIM_RATE_WINDOW_DEFAULT = 3600.0


def _chain_record_snapshot(record: reg.ChainRecord) -> dict:
    """Small, JSON-safe projection of a ChainRecord for UI payloads."""
    return {
        "chain_id": record.chain_id,
        "session": record.session,
        "role_a": record.role_a,
        "role_b": record.role_b,
        "status": record.status,
        "pid": record.pid,
        "current_round": record.current_round,
        "started_at": record.started_at,
        "last_activity": record.last_activity,
        "seed": record.seed,
    }


def _presence_snapshot(p: pc.ChainPresence) -> dict:
    """Dict form of a ChainPresence — the fields callers actually want."""
    return {
        "chain_id": p.chain_id,
        "focus": p.focus,
        "files_claimed": list(p.files_claimed),
        "started_at": p.started_at,
        "last_seen": p.last_seen,
    }


def _merge_chain(
    record: Optional[reg.ChainRecord],
    presence: Optional[pc.ChainPresence],
) -> dict:
    """Blend a registry row and a coordination presence for one chain.

    Either side can be None — both cases are real:
      - registry-only: chain just started, hasn't called register_chain on
        project_coordinator yet.
      - presence-only: coordination file outlived the chain process (dead
        process, crashed cleanup, etc).
    The merged dict always carries `has_registry` / `has_presence` flags so
    the caller can render the discrepancy rather than silently hiding it.
    """
    out: dict = {
        "has_registry": record is not None,
        "has_presence": presence is not None,
    }
    if record is not None:
        out.update(_chain_record_snapshot(record))
    if presence is not None:
        if record is None:
            out["chain_id"] = presence.chain_id
        out["focus"] = presence.focus
        out["files_claimed"] = list(presence.files_claimed)
        out["presence_last_seen"] = presence.last_seen
    else:
        out["focus"] = ""
        out["files_claimed"] = []
        out["presence_last_seen"] = None
    return out


def _project_key(record: reg.ChainRecord) -> str:
    """Bucket key for grouping — empty / None projects share one bucket."""
    return record.project.strip() if (record.project and record.project.strip()) else NO_PROJECT_KEY


def _compute_conflicts(
    presences: list[pc.ChainPresence],
) -> list[dict]:
    """Files claimed by 2+ presences in the same `presences` list.

    The caller has already filtered by ttl (stale presences don't appear),
    so a file showing up twice here is a genuine live-vs-live conflict.
    In normal operation `project_coordinator.claim_files()` refuses to add
    a claim that collides with a live chain, so this set is empty almost
    always — the surface exists to flag pathological state (direct JSON
    edits, schema migration bugs, a stale-then-revived chain that somehow
    kept its claim). Return shape is a list of dicts, sorted by file name
    for stable UI rendering:

        [{"file": "src/foo.py", "chains": ["chain-a", "chain-b"]}, ...]
    """
    owners: dict[str, list[str]] = {}
    for p in presences:
        for f in p.files_claimed:
            owners.setdefault(f, []).append(p.chain_id)
    conflicts = [
        {"file": f, "chains": sorted(ids)}
        for f, ids in owners.items()
        if len(ids) > 1
    ]
    conflicts.sort(key=lambda c: c["file"])
    return conflicts


def list_project_conflicts(
    project_dir: str | Path,
    ttl: float = pc.PRESENCE_TTL_DEFAULT,
) -> list[dict]:
    """Files claimed by 2+ live chains on the given project.

    Thin wrapper around `_compute_conflicts` that handles the
    coordination-file-missing and NO_PROJECT_KEY cases gracefully (both
    return an empty list — no coordination state = no way to conflict).
    """
    key = str(project_dir).strip() if project_dir else NO_PROJECT_KEY
    if key == NO_PROJECT_KEY:
        return []
    try:
        presences = pc.list_chains(Path(key), ttl=ttl)
    except (OSError, ValueError):
        return []
    return _compute_conflicts(presences)


def list_projects_with_chains(
    ttl: float = pc.PRESENCE_TTL_DEFAULT,
    claim_rate_window: float = CLAIM_RATE_WINDOW_DEFAULT,
) -> list[dict]:
    """Return one entry per project-with-at-least-one-chain, grouped.

    The result is the shape the grouped dashboard wants to render:

        [
          {"project": "/abs/path",
           "chain_count": 2,
           "active_count": 2,
           "focus_areas": ["backend", "tests"],
           "conflict_count": 0,
           "recent_claim_total": 5,
           "claim_rate_window_seconds": 3600.0,
           "chains": [ <merged chain dicts>, ... ]},
          ...
        ]

    Each merged chain dict carries an `in_conflict` bool (True iff at least
    one of its claimed files is also claimed by another live chain on this
    project) and a `recent_claims` int (count of ``claim`` activity entries
    this chain logged inside ``claim_rate_window``). The dashboard uses
    `in_conflict` to mark specific cards red and `recent_claims` to flag
    chains that are editing without calling the coordination CLI —
    per-project `conflict_count` and `recent_claim_total` drive the
    group-header badges.

    Sort order: projects with more active chains first, then alphabetical.
    Within a project, chains are sorted by started_at ascending so the
    longest-running pair renders at the top.

    `ttl` is forwarded to `project_coordinator` for presence liveness —
    stale presence rows don't contribute focus/claims to the merged chain.
    `claim_rate_window` is forwarded to ``project_coordinator.claim_rate``.
    """
    records = reg.list_chains()

    # Group chain records by project key. Non-project chains still appear
    # under (no project) so an operator doesn't lose track of them.
    groups: dict[str, list[reg.ChainRecord]] = {}
    for r in records:
        key = _project_key(r)
        groups.setdefault(key, []).append(r)

    # For each project directory, fetch the presence list once and index by
    # chain_id. Missing coordination file is a normal case (no chain on the
    # project has called register_chain yet) — treat as empty list.
    output: list[dict] = []
    for project_key, project_records in groups.items():
        presences: dict[str, pc.ChainPresence] = {}
        presence_list: list[pc.ChainPresence] = []
        claim_rates: dict[str, int] = {}
        if project_key != NO_PROJECT_KEY:
            try:
                presence_list = pc.list_chains(Path(project_key), ttl=ttl)
                for p in presence_list:
                    presences[p.chain_id] = p
            except (OSError, ValueError):
                # Unreadable coordination file shouldn't blank the dashboard.
                presences = {}
                presence_list = []
            try:
                claim_rates = pc.claim_rate(
                    Path(project_key), window_seconds=claim_rate_window,
                )
            except (OSError, ValueError):
                # Same failure mode as the presence read; keep the project
                # visible with zero counts rather than hiding it.
                claim_rates = {}

        conflicts = _compute_conflicts(presence_list)
        conflicted_chain_ids = {
            cid for c in conflicts for cid in c["chains"]
        }

        merged: list[dict] = []
        for record in sorted(project_records, key=lambda r: r.started_at):
            entry = _merge_chain(record, presences.get(record.chain_id))
            entry["in_conflict"] = entry.get("chain_id") in conflicted_chain_ids
            entry["recent_claims"] = claim_rates.get(entry.get("chain_id", ""), 0)
            merged.append(entry)

        # Also surface presences that aren't in the registry — these are
        # stale entries, but users want to see them so they can clean up.
        registry_ids = {r.chain_id for r in project_records}
        for pid_key, presence in presences.items():
            if pid_key in registry_ids:
                continue
            entry = _merge_chain(None, presence)
            entry["in_conflict"] = pid_key in conflicted_chain_ids
            entry["recent_claims"] = claim_rates.get(pid_key, 0)
            merged.append(entry)

        active = sum(
            1 for m in merged
            if m.get("status") in ("running", "resetting", "starting")
        )
        focus_areas = sorted({
            m.get("focus", "").strip()
            for m in merged if m.get("focus", "").strip()
        })

        output.append({
            "project": project_key,
            "chain_count": len(merged),
            "active_count": active,
            "focus_areas": focus_areas,
            "conflict_count": len(conflicts),
            "recent_claim_total": sum(claim_rates.values()),
            "claim_rate_window_seconds": claim_rate_window,
            "chains": merged,
        })

    output.sort(key=lambda d: (-d["active_count"], d["project"]))
    return output


def get_project_detail(
    project_dir: str | Path,
    ttl: float = pc.PRESENCE_TTL_DEFAULT,
    activity_limit: int = 50,
    claim_rate_window: float = CLAIM_RATE_WINDOW_DEFAULT,
) -> dict:
    """Full state for one project: chains, claims, recent activity, conflicts.

    Shape:
        {"project": "/abs/path",
         "chains": [<merged>...],  # each carries `in_conflict`, `recent_claims`
         "claims": {"path/to/file.py": "chain-id"},
         "conflicts": [{"file": "path/to/file.py", "chains": ["a", "b"]}],
         "activity": [{"chain_id": ..., "timestamp": ..., "kind": ...}, ...],
         "claim_rates": {"chain-id": 3, ...},
         "claim_rate_window_seconds": 3600.0}

    `project_dir` is whatever string the caller has — absolute path, relative
    path, or the `NO_PROJECT_KEY` sentinel for chains with no project. For
    the sentinel we report only the registry side; the coordination file
    only exists inside real project directories.
    """
    key = str(project_dir).strip() if project_dir else NO_PROJECT_KEY
    records = [r for r in reg.list_chains() if _project_key(r) == key]

    presences: list[pc.ChainPresence] = []
    claims: dict[str, str] = {}
    activity: list[dict] = []
    claim_rates: dict[str, int] = {}

    if key != NO_PROJECT_KEY:
        path = Path(key)
        try:
            presences = pc.list_chains(path, ttl=ttl)
        except (OSError, ValueError):
            presences = []
        try:
            for p in presences:
                for f in p.files_claimed:
                    # If two chains somehow both own the same file, the first
                    # one in the presence iteration wins — matches what
                    # project_coordinator.file_owner() would return. The
                    # overlap itself is surfaced separately via `conflicts`.
                    claims.setdefault(f, p.chain_id)
        except AttributeError:
            pass
        try:
            activity = [e.to_dict() for e in pc.read_activity(path, limit=activity_limit)]
        except (OSError, ValueError):
            activity = []
        try:
            claim_rates = pc.claim_rate(path, window_seconds=claim_rate_window)
        except (OSError, ValueError):
            claim_rates = {}

    conflicts = _compute_conflicts(presences)
    conflicted_ids = {cid for c in conflicts for cid in c["chains"]}

    presence_by_id = {p.chain_id: p for p in presences}
    merged: list[dict] = []
    for record in sorted(records, key=lambda r: r.started_at):
        entry = _merge_chain(record, presence_by_id.get(record.chain_id))
        entry["in_conflict"] = entry.get("chain_id") in conflicted_ids
        entry["recent_claims"] = claim_rates.get(entry.get("chain_id", ""), 0)
        merged.append(entry)

    registry_ids = {r.chain_id for r in records}
    for pid_key, presence in presence_by_id.items():
        if pid_key in registry_ids:
            continue
        entry = _merge_chain(None, presence)
        entry["in_conflict"] = pid_key in conflicted_ids
        entry["recent_claims"] = claim_rates.get(pid_key, 0)
        merged.append(entry)

    return {
        "project": key,
        "chains": merged,
        "claims": claims,
        "conflicts": conflicts,
        "activity": activity,
        "claim_rates": dict(claim_rates),
        "claim_rate_window_seconds": claim_rate_window,
    }


def project_summary_text(
    project_dir: str | Path,
    ttl: float = pc.PRESENCE_TTL_DEFAULT,
) -> str:
    """Plain-text snapshot for a project — thin pass-through to coordinator.

    Returning a friendly string for the NO_PROJECT_KEY case keeps callers
    from branching on that sentinel themselves.
    """
    key = str(project_dir).strip() if project_dir else NO_PROJECT_KEY
    if key == NO_PROJECT_KEY:
        return "(chains with no project have no coordination file)"
    try:
        return pc.render_summary(Path(key), ttl=ttl)
    except (OSError, ValueError) as e:
        return f"(could not read coordination for {key}: {e})"
