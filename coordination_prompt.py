"""Build the CLAUDE.md addendum that teaches in-chain agents about project_coordinator.

chain.py writes a CLAUDE.md into each pane's working directory; the in-chain
`claude` process reads it on startup. When multiple chains run on one project
they need to see each other's focus areas and file claims so they don't clobber
each other's edits. This module assembles the "multi-chain coordination" section
of that CLAUDE.md.

The section tells the agent:

- Their own chain_id (so they show up correctly in the coordination log).
- The project's coordination file location.
- Concrete ``python3 <project_coordinator.py>`` invocations to inspect state
  and claim files. The commands use the *absolute* path to the module script,
  not ``python3 -m project_coordinator`` — the pane's cwd is outside the
  dialectic repo (``<project>/.dialectic-a-<chain_id>/``), so ``-m`` can't
  resolve the module without a PYTHONPATH the agent doesn't have. Spelling
  out the script path is the only way a bare ``python3 ...`` works from
  wherever the agent happens to be.
- The focus-area convention (short free-text describing what this pair owns).
- A snapshot of current state (embedded via ``project_coordinator.render_summary``).

Design notes
------------

- Pure text builder. No registration, heartbeats, or mutations. Callers that
  want to register/heartbeat call ``project_coordinator`` directly.
- Intended as a one-line integration point in ``chain.py._write_pane_claude_md``:
  append ``coordination_prompt.claude_md_section(...)`` to the list of
  sections. The wiring itself is a separate segment.
- Degrades gracefully when the coordination file doesn't exist yet — the first
  chain on a project still gets taught the protocol, just with an empty state
  snapshot.

Public API
----------

- ``claude_md_section(project_dir, chain_id, focus="", include_summary=True)``:
  the full markdown string.
- ``claude_md_section_from_state(project_dir, chain_id, summary, focus="")``:
  testing seam that accepts a pre-computed summary string.
"""

from __future__ import annotations

from pathlib import Path

import project_coordinator as pc


# Absolute path to the project_coordinator.py script, resolved once at import.
# We hand this to the in-chain agent as the command prefix so the protocol
# works from any pane cwd — ``python3 -m project_coordinator`` would need the
# dialectic repo on PYTHONPATH, which the spawned `claude` process does not
# have. ``python3 /abs/path/to/project_coordinator.py`` has no such dependency.
_PC_SCRIPT = Path(pc.__file__).resolve()


_PROTOCOL_BODY = """\
### You MUST claim files before editing them

The claim system is cooperative — it reports conflicts, it does not \
enforce exclusion — so the protocol only works if you actually run the \
commands. Treat these steps as mandatory, not as suggestions.

The command prefix below uses the *absolute* path to the coordinator \
script (`{pc_script}`). Use it verbatim — your working directory is \
`<project>/.dialectic-a-{chain_id}/` (or `-b-`), which is not on any \
PYTHONPATH, so `python3 -m project_coordinator` will fail with \
`No module named project_coordinator`. Paste the commands exactly as \
written.

1. **Before any edit**, inspect who holds what on the project:

   ```bash
   python3 {pc_script} --project {project_dir} --summary
   python3 {pc_script} --project {project_dir} --claims
   ```

2. **Claim the files you're about to modify** (pass one or more paths, \
relative to `{project_dir}` or absolute — the coordinator stores them \
verbatim, so be consistent):

   ```bash
   python3 {pc_script} --project {project_dir} \\
       --claim path/to/file1.py path/to/file2.py --chain {chain_id}
   ```

   - Exit 0 means the claim succeeded — proceed with the edit.
   - Exit 2 means another live chain already holds one of the files. \
Pick different work, note it in your plan file (`plan-{chain_id}.md`), \
and try again later. **Do not** edit a file owned by a different chain.

3. **After you've finished and committed an edit**, release the claim so \
other chains can pick the file up:

   ```bash
   python3 {pc_script} --project {project_dir} \\
       --release path/to/file1.py path/to/file2.py --chain {chain_id}
   ```

   Pass `--release` with no files to drop every file this chain is \
holding (useful when you're pivoting to a totally different area).

Your chain id is `{chain_id}`. Use exactly that string for `--chain` — \
the coordinator namespaces claims by id, so a typo here leaves a \
"ghost" claim that other chains will then avoid.

### Focus areas

Each chain's focus is a short free-text description of what that pair \
is working on ("backend api", "tests for auth", "frontend polish"). \
Keep your focus disjoint from the other active chains' focuses — the \
current state snapshot at the top of this section lists them. If your \
focus isn't set, either infer it from the seed or ask the operator. \
Don't guess into a focus that overlaps another chain."""


def _focus_sentence(focus: str) -> str:
    """The single line that describes this chain's focus."""
    if focus:
        return f"Your focus area is: {focus!r}."
    return (
        "Your focus area is not set yet. Read the coordination state "
        "block below and pick a focus disjoint from what other chains "
        "are already on."
    )


def _header(project_dir: Path, chain_id: str, focus: str) -> str:
    return (
        f"You are chain `{chain_id}` working in `{project_dir}`. Other chains "
        "may be running on the same project in parallel. You MUST coordinate "
        "with them via `project_coordinator` before any file edit — the "
        "protocol below is not optional.\n\n"
        f"{_focus_sentence(focus)}"
    )


def claude_md_section_from_state(
    project_dir: Path,
    chain_id: str,
    summary: str,
    focus: str = "",
    pc_script: Path | str | None = None,
) -> str:
    """Build the section from a pre-computed coordination summary string.

    The state snapshot comes before the protocol body so an agent sees
    *what's happening right now* before reading *how to participate*.
    Testing seam: pytest hands in whatever summary text it wants
    embedded under "Current coordination state" rather than writing a
    real coordination.json.

    ``pc_script`` overrides the absolute path to ``project_coordinator.py``
    that gets interpolated into the protocol commands. Defaults to the
    real module location (``Path(project_coordinator.__file__).resolve()``);
    tests may override to pin rendering against a stable fixture path.
    """
    project_dir = Path(project_dir)
    script_path = Path(pc_script) if pc_script is not None else _PC_SCRIPT
    parts: list[str] = [
        "## Multi-chain coordination",
        "",
        _header(project_dir, chain_id, focus),
    ]
    if summary:
        parts.extend([
            "",
            "### Current coordination state",
            "",
            "```",
            summary.rstrip("\n"),
            "```",
        ])
    parts.extend([
        "",
        _PROTOCOL_BODY.format(
            project_dir=project_dir,
            chain_id=chain_id,
            pc_script=script_path,
        ),
    ])
    return "\n".join(parts)


def claude_md_section(
    project_dir: Path,
    chain_id: str,
    focus: str = "",
    include_summary: bool = True,
    pc_script: Path | str | None = None,
) -> str:
    """Build the "Multi-chain coordination" section for CLAUDE.md.

    When ``include_summary`` is True (default) this calls
    ``project_coordinator.render_summary`` to embed the current state. A
    missing/unreadable coordination file is caught and replaced with a short
    placeholder — the protocol itself still reaches the agent.

    Set ``include_summary=False`` when the caller has already rendered the
    summary separately or is writing into a brand-new project where an
    empty-state snapshot would be noise.

    ``pc_script`` overrides the coordinator script path interpolated into
    the protocol commands; defaults to ``project_coordinator.__file__``.
    """
    project_dir = Path(project_dir)
    if include_summary:
        try:
            summary = pc.render_summary(project_dir)
        except Exception:
            # Don't let a corrupt coordination file break CLAUDE.md generation.
            # The protocol body is what the agent needs most; the snapshot is a
            # convenience. Swallowing the error here is deliberate.
            summary = "(coordination state not available)"
    else:
        summary = ""
    return claude_md_section_from_state(
        project_dir=project_dir,
        chain_id=chain_id,
        summary=summary,
        focus=focus,
        pc_script=pc_script,
    )
