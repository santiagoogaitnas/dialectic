"""Pre-answer Claude Code's one-time dialogs so panes boot unattended.

Claude Code stores one-time acceptance state in an internal config file
(``$CLAUDE_CONFIG_DIR/.claude.json`` when that env var is set, else
``~/.claude.json``). Three gates in that file can block a fresh
Dialectic install: the per-folder "Do you trust the files in this
folder?" dialog, the once-per-machine ``--dangerously-skip-permissions``
acceptance, and first-run onboarding. Each renders an interactive prompt
in the pane, and the relay drives the pane blind -- nobody is there to
click, so the chain stalls before round 1.

Trust is checked against the folder *and its parents*, so seeding the
project root once covers every ``.dialectic-*-<chain-id>`` scratch dir
created beneath it: one config entry per project, not per chain.

The config file is Claude Code's own live state -- it rewrites it
continuously while running -- so every update here is defensive:
sidecar lockfile, re-read under the lock, merge (never replace), atomic
temp-file + rename, 0600 permissions, and no write at all when the
existing content doesn't parse. Seeding happens once per launch, before
any pane starts, which also keeps this write out of the window where
the panes' own claude processes are rewriting the file.

The key names are undocumented internal state and can drift between
Claude Code versions. They are centralized in the constants below so a
rename upstream is a one-place fix here; a stale key name degrades to
the old behavior (the dialog appears and the launch stalls -- see the
README FAQ), it does not corrupt anything.
"""

import fcntl
import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger("claude_config")

# Per-project flag, stored under projects["<abs path>"].
TRUST_KEY = "hasTrustDialogAccepted"

# Top-level flag: the once-per-machine acceptance of
# --dangerously-skip-permissions (which start_agent always passes).
BYPASS_KEY = "bypassPermissionsModeAccepted"

# Written only when the config file does not exist at all, i.e. the
# machine has never run claude: skips the theme/welcome onboarding
# screens. On any machine that has run claude even once, these are
# already set and we leave them alone.
FRESH_INSTALL_KEYS = {
    "hasCompletedOnboarding": True,
    "theme": "dark",
    "numStartups": 1,
}


def config_path() -> Path:
    """The .claude.json the panes' claude processes will read.

    Mirrors start_agent's env forwarding: when CLAUDE_CONFIG_DIR is set
    it is passed into the pane, and claude then reads
    $CLAUDE_CONFIG_DIR/.claude.json; otherwise ~/.claude.json.
    """
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir).expanduser() / ".claude.json"
    return Path.home() / ".claude.json"


def _locked_update(config_file: Path, mutate) -> bool:
    """Apply ``mutate`` to the parsed config under a lock, atomically.

    ``mutate(data, existed)`` edits the dict in place and returns True
    if anything changed (False skips the write entirely). Returns True
    when the file is in the desired state, False on any failure --
    including unparseable existing content, which is never overwritten.
    """
    lock_path = config_file.with_name(config_file.name + ".dialectic.lock")
    try:
        config_file.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                existed = config_file.exists()
                data = {}
                if existed:
                    data = json.loads(config_file.read_text(encoding="utf-8"))
                    if not isinstance(data, dict):
                        logger.warning(
                            f"{config_file} is not a JSON object; leaving it alone."
                        )
                        return False

                if not mutate(data, existed):
                    return True

                fd, tmp_name = tempfile.mkstemp(
                    dir=str(config_file.parent), prefix=".claude.json."
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(data, f)
                    os.chmod(tmp_name, 0o600)
                    os.replace(tmp_name, config_file)
                except BaseException:
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass
                    raise
                return True
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except json.JSONDecodeError as e:
        logger.warning(
            f"Could not parse {config_file} ({e}); leaving it untouched."
        )
        return False
    except OSError as e:
        logger.warning(f"Could not update {config_file}: {e}")
        return False


def _stale_scratch_keys(projects: dict, root: Path, keep_chain_ids) -> list:
    """Scratch-dir entries under ``root`` whose chain is no longer live.

    A key qualifies when it is exactly ``<root>/.dialectic-a-<id>`` or
    ``.../.dialectic-b-<id>`` (same naming as
    registry.ChainConfig.pane_dirs) and ``<id>`` is not in
    ``keep_chain_ids``. Anything deeper or shaped differently is left
    alone.
    """
    stale = []
    for side in ("a", "b"):
        prefix = str(root / f".dialectic-{side}-")
        for key in projects:
            if key.startswith(prefix):
                chain_id = key[len(prefix):]
                if chain_id and "/" not in chain_id and chain_id not in keep_chain_ids:
                    stale.append(key)
    return stale


def seed_first_run_keys(
    project_root: Path,
    config_file: Path | None = None,
    active_chain_ids=None,
) -> bool:
    """Mark the one-time dialogs as already accepted, before any pane boots.

    Seeds folder trust for the resolved project root (parent-dir trust
    then covers the per-chain scratch dirs beneath it) and the global
    bypass-mode acceptance. Onboarding keys are written only when the
    config file doesn't exist yet. Merge-only for everything else:
    nothing existing is removed or replaced. Returns False when the
    config couldn't be updated -- the launch should proceed anyway, it
    just may stall on the interactive dialog this was meant to suppress.

    ``active_chain_ids``, when given, is the set of chains still live on
    this project: any ``.dialectic-*-<id>`` entry under the project root
    for a chain NOT in the set is swept out in the same write. This is
    the reliable half of scratch-entry cleanup -- the prune at chain end
    (remove_chain_scratch_entries) races against the dying panes' claude
    processes flushing their own config state back, and after Ctrl+C the
    panes are deliberately left alive. At launch time the losers of
    those races are long dead, so the sweep is safe and self-healing.
    """
    if config_file is None:
        config_file = config_path()
    # resolve() so the key matches the path claude computes for the
    # pane's cwd (symlinks folded, e.g. /tmp -> /private/tmp on macOS).
    root = Path(project_root).resolve()
    root_str = str(root)

    def mutate(data: dict, existed: bool) -> bool:
        changed = False
        projects = data.setdefault("projects", {})
        if not isinstance(projects, dict):
            logger.warning(
                f"'projects' in {config_file} is not an object; not seeding trust."
            )
            return False
        entry = projects.get(root_str)
        if not isinstance(entry, dict):
            entry = {}
            projects[root_str] = entry
            changed = True
        if entry.get(TRUST_KEY) is not True:
            entry[TRUST_KEY] = True
            changed = True
        if data.get(BYPASS_KEY) is not True:
            data[BYPASS_KEY] = True
            changed = True
        if not existed:
            for key, value in FRESH_INSTALL_KEYS.items():
                data.setdefault(key, value)
            changed = True
        if active_chain_ids is not None:
            for key in _stale_scratch_keys(projects, root, set(active_chain_ids)):
                del projects[key]
                changed = True
        return changed

    return _locked_update(config_file, mutate)


def remove_chain_scratch_entries(
    project_root: str | Path, chain_id: str, config_file: Path | None = None
) -> bool:
    """Drop the config entries claude created for a finished chain's panes.

    Each pane boot makes claude record an entry for its cwd -- the
    per-chain scratch dirs from registry.ChainConfig.pane_dirs(), which
    are never reused after the chain ends. Without this, the file grows
    two dead entries per launch, forever. Only keys matching this
    chain's exact scratch paths are touched; the project-root trust
    entry seeded at launch stays.
    """
    if config_file is None:
        config_file = config_path()
    if not config_file.exists():
        return True
    root = Path(project_root).resolve()
    # Same naming as registry.ChainConfig.pane_dirs().
    targets = {
        str(root / f".dialectic-a-{chain_id}"),
        str(root / f".dialectic-b-{chain_id}"),
    }

    def mutate(data: dict, existed: bool) -> bool:
        projects = data.get("projects")
        if not isinstance(projects, dict):
            return False
        removed = False
        for target in targets:
            if target in projects:
                del projects[target]
                removed = True
        return removed

    return _locked_update(config_file, mutate)
