"""Chain-side bridge to `project_coordinator`.

`project_coordinator.py` is the pure state primitive for project-level
coordination: read a JSON file, write a JSON file, take a lock. This
module is the chain's *lifecycle* wrapper around that primitive.

What it adds on top of the underlying API
-----------------------------------------

- A context manager (`ChainCoordinatorContext`) that registers on entry
  and deregisters on exit, so chains can't forget to clean up.
- A background heartbeat thread that keeps the chain's presence fresh
  without the loop needing to remember to call `heartbeat()` manually.
- Exception isolation: every call into `project_coordinator` is wrapped
  so coordination failures (disk full, corrupted JSON, unreadable lock
  file) never propagate up to the chain's relay loop. The chain's job
  is to keep talking; a broken coord file should degrade visibility for
  the dashboard, not kill the chain.
- Thin pass-throughs for the useful operations (`set_focus`,
  `claim_files`, `release_files`, `file_owner`, `append_note`,
  `project_summary`) so the caller imports one name and gets the whole
  surface.

What it deliberately does NOT do
--------------------------------

- It doesn't decide policy. If `claim_files()` reports conflicts, the
  caller handles it. If a file is already owned, this module doesn't
  force-take it.
- It doesn't duplicate the state. The coordination file is the single
  source of truth; this object holds a reference, not a cache.
- It doesn't call `register_chain` on the *chain registry*
  (`registry.py`). That's a separate concern — process/tmux tracking —
  handled by the caller.

Integration point for chain.py
------------------------------

The intended shape once wired into `run_chain()`:

    cfg = reg.ChainConfig(...)
    with ChainCoordinatorContext(
        project_dir=cfg.project_dir,
        chain_id=cfg.chain_id,
        focus=cfg.focus or "",
    ) as coord:
        ...
        coord.set_focus("backend rewrite")
        ...

One import, one `with` block, zero fuss.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Iterable, Optional

import project_coordinator as pc

logger = logging.getLogger("chain_coordinator")

# Default cadence for the background heartbeat. `project_coordinator`'s
# default TTL is 600s, so heartbeating every 60s keeps every chain at
# least 540s away from being treated as stale. Tune via the constructor
# when testing or when the ambient TTL changes.
HEARTBEAT_INTERVAL_DEFAULT = 60.0


class _HeartbeatThread(threading.Thread):
    """Daemon thread that calls `pc.heartbeat` every `interval` seconds.

    Stops when the paired Event is set. Uses `Event.wait(timeout)` rather
    than `time.sleep` so shutdown is prompt — a chain exiting shouldn't
    hang for a minute waiting for the next tick.

    All heartbeat calls are wrapped so a transient filesystem error
    (disk full, permissions, concurrent corruption) just logs and
    continues. Dropping one heartbeat is fine; presence TTL is long
    enough to survive an outage.
    """

    def __init__(
        self,
        project_dir: Path,
        chain_id: str,
        interval: float,
        stop_event: threading.Event,
    ) -> None:
        super().__init__(
            name=f"coord-heartbeat-{chain_id}",
            daemon=True,
        )
        self._project_dir = project_dir
        self._chain_id = chain_id
        self._interval = interval
        self._stop = stop_event

    def run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                pc.heartbeat(self._project_dir, self._chain_id)
            except Exception as e:  # noqa: BLE001 — defend the relay loop
                logger.warning(
                    "Heartbeat failed for %s on %s: %s",
                    self._chain_id, self._project_dir, e,
                )


class ChainCoordinatorContext(AbstractContextManager):
    """Lifecycle manager for one chain's presence on a project.

    Register-on-enter / deregister-on-exit. Starts a background heartbeat
    thread so the chain's presence doesn't age out during long work
    cycles. Every pass-through method swallows exceptions and returns a
    sentinel so the relay loop never has to worry about coord failures.

    Construction parameters
    -----------------------
    project_dir: the directory containing (or that will contain) the
      `.dialectic/coordination.json` file. If None or not a directory,
      the context becomes a no-op — every method returns its failure
      sentinel, nothing is registered, no heartbeat runs. This lets
      callers wrap a chain unconditionally without branching on
      project-vs-no-project at the call site.
    chain_id: the chain's registry id. Required in active mode.
    focus: initial focus string (free-form, e.g. "backend api"). Empty
      string is fine — agents can set it later via `set_focus()`.
    heartbeat_interval: seconds between heartbeat calls. Defaults to 60s.
      Pass 0 or a negative number to disable the background thread (still
      useful when tests want deterministic control).
    """

    def __init__(
        self,
        project_dir: Optional[Path | str],
        chain_id: str,
        focus: str = "",
        heartbeat_interval: float = HEARTBEAT_INTERVAL_DEFAULT,
    ) -> None:
        self._project_dir: Optional[Path] = None
        if project_dir is not None:
            p = Path(project_dir)
            if p.is_dir():
                self._project_dir = p
        self._chain_id = chain_id
        self._focus = focus
        self._heartbeat_interval = heartbeat_interval
        self._stop_event = threading.Event()
        self._thread: Optional[_HeartbeatThread] = None
        self._registered = False

    @property
    def active(self) -> bool:
        """True iff this context will actually talk to the coordination file.

        False when no project directory was provided, or the path wasn't
        a real directory at construction time. Inactive contexts are
        fine to use — their methods just do nothing.
        """
        return self._project_dir is not None

    @property
    def project_dir(self) -> Optional[Path]:
        """The resolved project directory, or None if the context is inactive."""
        return self._project_dir

    @property
    def chain_id(self) -> str:
        return self._chain_id

    # --- Context manager ---

    def __enter__(self) -> "ChainCoordinatorContext":
        if not self.active:
            return self
        if not self._chain_id:
            logger.warning(
                "ChainCoordinatorContext enter with empty chain_id; skipping register.",
            )
            return self
        try:
            pc.register_chain(
                self._project_dir, self._chain_id, focus=self._focus,
            )
            self._registered = True
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "register_chain failed for %s on %s: %s",
                self._chain_id, self._project_dir, e,
            )

        if self._registered and self._heartbeat_interval > 0:
            self._thread = _HeartbeatThread(
                project_dir=self._project_dir,
                chain_id=self._chain_id,
                interval=self._heartbeat_interval,
                stop_event=self._stop_event,
            )
            self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # Signal the heartbeat thread first so it stops promptly, then
        # deregister. The deregister call tolerates the case where the
        # chain was never registered (e.g., register_chain raised at
        # __enter__) by checking pc.get_chain first — a no-op deregister
        # shouldn't raise.
        self._stop_event.set()
        if self._thread is not None:
            # Give the thread a brief window to exit; daemon=True means
            # we don't have to wait forever on a stuck thread.
            self._thread.join(timeout=2.0)
            self._thread = None
        if not self.active or not self._registered:
            return None
        try:
            pc.deregister_chain(self._project_dir, self._chain_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "deregister_chain failed for %s on %s: %s",
                self._chain_id, self._project_dir, e,
            )
        self._registered = False
        return None

    # --- Pass-throughs ---

    def heartbeat(self) -> bool:
        """Force a manual heartbeat (in addition to the background thread).

        Returns True on success, False on any coord failure. The
        background thread keeps presence fresh on its own, so most
        callers will not need this. It exists for tests and for the rare
        case where a long synchronous operation (a 10-minute curator
        call) wants to guarantee a fresh timestamp on exit.
        """
        if not self.active:
            return False
        try:
            return bool(pc.heartbeat(self._project_dir, self._chain_id))
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "heartbeat failed for %s on %s: %s",
                self._chain_id, self._project_dir, e,
            )
            return False

    def set_focus(self, focus: str) -> bool:
        """Update the chain's focus string. Returns True on success.

        Empty strings are allowed (clears focus). The underlying API
        records a 'focus' activity entry so other chains watching the
        log can see what shifted.
        """
        if not self.active or not self._registered:
            return False
        try:
            ok = bool(pc.set_focus(self._project_dir, self._chain_id, focus))
            if ok:
                self._focus = focus
            return ok
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "set_focus failed for %s on %s: %s",
                self._chain_id, self._project_dir, e,
            )
            return False

    @property
    def focus(self) -> str:
        """Last focus string we asked project_coordinator to record."""
        return self._focus

    def claim_files(self, files: Iterable) -> tuple[bool, list[str]]:
        """Try to claim `files`. Returns (True, []) on success.

        On conflict returns (False, [conflicting_files]) so callers can
        decide to wait, pick different files, or note the conflict. If
        the context is inactive or the chain never registered, returns
        (False, []) — nothing was claimed, and there's no useful
        conflict list to surface either.
        """
        if not self.active or not self._registered:
            return (False, [])
        try:
            return pc.claim_files(self._project_dir, self._chain_id, files)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "claim_files failed for %s on %s: %s",
                self._chain_id, self._project_dir, e,
            )
            return (False, [])

    def release_files(self, files: Optional[Iterable] = None) -> int:
        """Release `files` (or all held files when None). Returns count."""
        if not self.active or not self._registered:
            return 0
        try:
            return int(pc.release_files(self._project_dir, self._chain_id, files))
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "release_files failed for %s on %s: %s",
                self._chain_id, self._project_dir, e,
            )
            return 0

    def file_owner(self, file_path) -> Optional[str]:
        """Return chain_id of the file's owner, or None (including on failure).

        Callers use this to ask "should I edit this?" before writing.
        Not a hard lock — the underlying system is cooperative.
        """
        if not self.active:
            return None
        try:
            return pc.file_owner(self._project_dir, file_path)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "file_owner failed for %s on %s: %s",
                self._chain_id, self._project_dir, e,
            )
            return None

    def append_note(self, summary: str, files: Optional[Iterable] = None) -> bool:
        """Record a free-form activity entry. Returns True on success.

        Useful for marking 'finished backend auth' or 'found bug in X'
        without having to formally release/claim. The entry shows up in
        `project_summary()` so other chains can see the signal.
        """
        if not self.active:
            return False
        try:
            pc.append_note(
                self._project_dir, self._chain_id, summary, files=files,
            )
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "append_note failed for %s on %s: %s",
                self._chain_id, self._project_dir, e,
            )
            return False

    def project_summary(self) -> str:
        """Render the human-readable project summary. Empty string on failure.

        Thin wrapper so callers don't need to import `project_coordinator`
        just for the render helper. The string is suitable to embed in
        a CLAUDE.md update, a UI panel, or an agent's recap.
        """
        if not self.active:
            return ""
        try:
            return pc.render_summary(self._project_dir)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "render_summary failed on %s: %s", self._project_dir, e,
            )
            return ""

    def other_chains(self) -> list[pc.ChainPresence]:
        """Return the list of other live chains on this project.

        Excludes `self` so the caller can loop over "who else is here"
        without filtering. Empty list on coord failure — treating the
        project as if no one else is there is strictly less dangerous
        than pretending someone is when we don't actually know.
        """
        if not self.active:
            return []
        try:
            all_chains = pc.list_chains(self._project_dir)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "list_chains failed on %s: %s", self._project_dir, e,
            )
            return []
        return [c for c in all_chains if c.chain_id != self._chain_id]
