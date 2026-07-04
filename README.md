# Dialectic

[![Release](https://img.shields.io/github/v/release/santiagoogaitnas/dialectic)](https://github.com/santiagoogaitnas/dialectic/releases/latest)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![Dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen)
[![License: MIT](https://img.shields.io/github/license/santiagoogaitnas/dialectic)](LICENSE)

Two Claude Code agents working on your project in a continuous back-and-forth loop that can run indefinitely. A third process (the curator) periodically distills their conversation into a dense recap and restarts them from it, so the agents never hit the context wall and never degrade.

<!-- DEMO SLOT — replace this comment with the screencast when recorded.
     Suggested scene (~30s): run `python3 chain.py "..." --project ...`, show the
     launch card, then `tmux attach` with both agents talking side by side, then
     the dashboard at localhost:8420. Record with `vhs` or asciinema→gif, keep it
     under ~10 MB, commit it to assets/demo.gif, then embed here:
     ![Two agents in a Dialectic chain](assets/demo.gif)
-->

- **Long unattended work sessions.** Chains run until you stop them. Context resets are automatic, so round 40 is as sharp as round 4.
- **Two perspectives instead of one.** Each agent's output is challenged by a counterpart with a different role, which catches drift and bad ideas earlier than a single agent working alone.
- **Everything on disk.** The full conversation log, the curator's recaps, and the agents' plan file survive crashes and restarts. You can stop a chain and relaunch it, and the agents pick up from the files they left behind.

## What it does

You point Dialectic at a project directory and give it a starting instruction. It opens a tmux session with two Claude Code agents in it — by default a pragmatic builder and a pattern thinker — and relays each agent's response to the other. Both agents have full tool access inside your project: they read code, write code, run tests, and keep a shared plan file up to date.

The problem with long agent sessions is that context fills up and quality drops. Dialectic's answer is the curator: every 5 rounds, a separate Claude process reads both transcripts, writes a dense recap for each agent, clears both sessions, and injects the recaps. The agents resume with fresh context and keep going. The loop only ends when you stop it.

## Requirements

- macOS or Linux (Windows works via WSL2 — see FAQ)
- Python 3.10+ (standard library only — no packages to install)
- [tmux](https://github.com/tmux/tmux)
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code), installed and authenticated (`claude` must work in your terminal)

## Get started

Once:

```bash
git clone https://github.com/santiagoogaitnas/dialectic.git
cd dialectic
```

**1. Start a chain** — one command: your instruction, and the project the agents work on.

```bash
python3 chain.py "tighten up the onboarding flow" --project ~/code/myapp
```

That runs the default pairing, builder + thinker. Want a different pairing? Same command, two flags:

```bash
python3 chain.py "tighten up the onboarding flow" --project ~/code/myapp \
    --role-a builder.txt --role-b contrarian.txt
```

(`python3 chain.py --list-roles` prints every role with a one-line preview.)

After a few seconds of setup it tells you what you have and what to type next:

```
Chain 07021530-ab12 launching.
  watch:  tmux attach -t chain-07021530-ab12    (run this in another terminal)
  stop:   Ctrl+C here, or: python3 chain.py --stop 07021530-ab12
This terminal runs the relay loop — leave it open.
```

**2. Watch the agents** — open a second terminal and paste the watch line:

```bash
tmux attach -t chain-07021530-ab12
```

You're now looking at the two agents side by side, talking to each other and working in your project. Close the tab (or detach with `Ctrl-b` then `d`) whenever you like — they keep going.

**3. Stop** — `Ctrl+C` in the launch terminal, or `python3 chain.py --stop <chain-id>` from anywhere. `python3 chain.py --list` shows every chain with its status and project.

Output lands in your project directory: whatever the agents build, plus a `plan-<chain-id>.md` working doc. Logs and curator notes live in `chainwork/<chain-id>/` inside this repo (`tail -f chainwork/<chain-id>/chain_log.md` follows the transcript).

## Web dashboard

```bash
python3 -m ui.server        # http://localhost:8420
```

Launch, monitor, and stop chains from the browser. Chains are grouped by project and update live. Each chain card opens a detail view with the streaming log, the plan file, and the curator's long-term notes. No dependencies — it runs on the Python standard library.

## Roles

Each chain runs two roles, one per agent. Defaults are `builder.txt` and `thinker.txt`. Swap either:

```bash
python3 chain.py "topic" --role-a builder.txt --role-b contrarian.txt
```

| Role | Character |
|------|-----------|
| `builder.txt` | Pragmatic. Small verified steps, minimum code that solves the problem. |
| `thinker.txt` | Pattern thinker. Pulls threads, pushes toward the insight beneath the surface. |
| `contrarian.txt` | Presses on the weakest point of whatever the counterpart said. |
| `architect.txt` | Structure first. Writes a contract before any module is built. |
| `reviewer.txt` | Reads the counterpart's code, runs it, hunts logic errors and edge cases. |
| `skeptic.txt` | Demands quotes and denominators. Rejects summaries of summaries. |
| `strategist.txt` | Audience, positioning, what the work is for. |
| `investigator.txt` | Hands-on data parsing. Works from evidence, not impressions. |
| `synthesizer.txt` | Turns raw findings into the big picture and a clean handoff. |

Pick two that press on each other from different angles. Roles are plain text files in `roles/` — add your own.

## Running several chains on one project

Each launch gets its own chain id, tmux session, and plan file, so you can run multiple chains against the same project:

```bash
python3 chain.py "rewrite the auth module" --project /path/to/project --focus backend &
python3 chain.py "shore up test coverage"  --project /path/to/project --focus tests &
```

Chains on the same project share a coordination file (`<project>/.dialectic/coordination.json`). Each chain advertises a focus area, claims files before editing them, and sees what the others are doing. The claim system is cooperative — it reports conflicts rather than enforcing locks. Inspect it from a shell:

```bash
python3 project_coordinator.py --project /path/to/project --summary
```

The default cap is 5 concurrent chains per project (`--max-chains` to change).

## How it works

```
Seed → Agent A → Agent B → Agent A → Agent B → ...
                                                 │
                              every 5 rounds:    │
                              curator recaps  ◄──┘
                              /clear both
                              inject recap
                              continue
```

1. Creates a tmux session with two panes, each running `claude` inside your project, each with a role prompt written to its `CLAUDE.md`.
2. Sends your seed to Agent A, extracts A's final response from the session transcript, injects it into B. B's response goes back to A. Repeat.
3. Every 5 rounds the curator (a separate `claude -p` call) recaps both transcripts, clears both sessions, and injects the recaps.
4. The curator also keeps a `bulletin.md` — observations that persist across resets and feed into future recaps.

Only Ctrl+C (or killing the tmux session) stops a chain. Empty responses, identical outputs, and curator failures all trigger recovery, not exit.

## FAQ

**How much does it cost to run?**
Chains are ordinary Claude Code sessions running under your existing authentication, so usage is billed however your Claude Code usage is billed (subscription plan or API key). Note that a chain left running generates continuous usage — two agents plus periodic curator calls.

**Is it safe to point at my code?**
The agents run with `--dangerously-skip-permissions`: they execute commands and edit files without asking. Only point a chain at a project you're comfortable letting an agent modify, keep the project in git so you can review and revert, and don't point it at directories with credentials or irreplaceable data. Launching is treated as consent: at launch Dialectic pre-accepts Claude Code's one-time folder-trust prompt for the target project and its bypass-mode acceptance, written directly into Claude Code's own config (`~/.claude.json`), so the unattended panes never sit on a dialog. It also removes the per-chain entries Claude Code leaves in that file once chains end.

**How do I stop everything?**
`python3 chain.py --stop <chain-id>`, or Ctrl+C in the terminal that launched the chain. The tmux session is left alive after Ctrl+C so you can inspect the agents; kill it with `tmux kill-session -t chain-<chain-id>`.

**A chain died / my machine rebooted. Did I lose the work?**
No. The agents' output is ordinary files in your project, and their plan file survives. Launch a new chain with the same command and the agents read what's already there and continue.

**The agents never started on my very first run.**
This shouldn't happen: at launch Dialectic pre-accepts Claude Code's one-time prompts (the "do you trust the files in this folder?" dialog and the `--dangerously-skip-permissions` acceptance) before the agents boot. If a pane still sits on an interactive prompt — most likely a Claude Code update changed how that acceptance is stored — attach to the tmux session (the launch output prints the exact command), accept the prompt in each pane, relaunch, and please open an issue. (Missing `tmux` or `claude` binaries are caught before launch with a clear error.)

**The loop stopped advancing but the agents look fine.**
Dialectic detects when an agent is done responding by watching Claude Code's terminal UI, and that UI changes between Claude Code versions. If idle detection breaks after a Claude Code update, that's the most likely cause — check `chainwork/<chain-id>/chain_log.md` to see where it stalled, and open an issue.

**What stops an agent from opening an interactive prompt nobody can answer?**
Each agent is told up front that its terminal is driven by an automated relay with no human watching: don't open interactive question prompts (state assumptions and keep going), and don't enter team/multi-terminal modes that take over the display, because the relay reads the screen and a takeover stalls the chain. This is an instruction rather than a hard block, so if a chain ever does stall this way, attach to the tmux session to see what the pane is showing, nudge it past, and open an issue.

**Does it work with the API instead of a subscription?**
It shells out to the `claude` CLI for everything. Any auth that makes `claude` work in your terminal works here.

**Does it work on Windows?**
Not natively — it depends on tmux and Unix file locking. It runs fine inside [WSL2](https://learn.microsoft.com/en-us/windows/wsl/): install tmux and Claude Code in the WSL environment, and keep your project directory inside the WSL filesystem (e.g. under `~/`) rather than on `/mnt/c`, where file locking is unreliable.

**How do I run the tests?**
`python3 -m pytest tests/` — no setup beyond pytest. `tests/test_smoke.py` makes a few real `claude` CLI calls to validate assumptions, so it needs an authenticated CLI; skip it with `--ignore=tests/test_smoke.py` if you don't want that.

## Project structure

| Path | What it is |
|------|------------|
| `chain.py` | The relay loop — injection, extraction, curator resets, logging |
| `registry.py` | Chain registry — ids, status, process tracking |
| `project_coordinator.py` | Per-project coordination file (focus areas, file claims) |
| `chain_coordinator.py` | Registers a chain with the coordinator for the life of the run |
| `coordination_prompt.py` | Teaches in-chain agents the coordination protocol via CLAUDE.md |
| `janitor/` | Idle detection, transcript reading, curator subprocess |
| `ui/` | Stdlib web dashboard |
| `roles/` | Role prompt files |
| `tests/` | Test suite |
| `chainwork/<chain-id>/` | Per-chain runtime output (log, bulletin) |

## License

MIT
