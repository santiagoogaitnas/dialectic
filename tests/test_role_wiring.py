"""The agent labels the curator and transcript use must follow the actual roles.

Before this, the curator was told it was reading "a pragmatic builder and a
pattern thinker", the recap labels were hardcoded "Builder (pragmatic)" /
"Thinker (patterns)", the bulletin said "BUILDER/THINKER DIALOGUE", and the
transcript headers said Builder/Thinker — no matter which --role-a/--role-b
a chain was launched with. A non-default pairing (architect + contrarian)
was therefore recapped and logged as a dialogue that wasn't happening, and
because the recap is the ONLY context an agent has after a reset, that
mislabelling compounded every 5 rounds.

These tests pin two things at once:
  1. The default pairing stays byte-stable — same dialogue headers and
     transcript labels the engine emitted before roles were threaded
     through (the "we didn't break the working case" guarantee).
  2. A custom or never-before-seen role file is carried through to the
     curator's recap label, the bulletin headers, and the transcript.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_project_root = str(Path(__file__).parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import chain
from janitor.jsonl_reader import JSONLCursor
from janitor.types import JanitorResult


# --- role_label derivation ---

@pytest.mark.parametrize("filename,expected", [
    ("builder.txt", "Builder"),
    ("thinker.txt", "Thinker"),
    ("architect.txt", "Architect"),
    ("contrarian.txt", "Contrarian"),
    ("research-director.txt", "Research Director"),  # hyphen -> space
    ("historian.txt", "Historian"),                  # invented, never in repo
    ("/abs/path/to/skeptic.txt", "Skeptic"),         # full path accepted
])
def test_role_label_derivation(filename, expected):
    assert chain.role_label(filename) == expected


def test_role_label_empty_stem_falls_back():
    # Defensive fallback (unreachable via the CLI, which resolves a real
    # role file first): an empty name never yields a blank label.
    assert chain.role_label("") == "Agent"


# --- curator system prompt no longer hardcodes the pair ---

def test_janitor_prompt_does_not_hardcode_builder_thinker():
    """The shared curator prompt must not assert a fixed pairing, or it
    contradicts the per-recap AGENT ROLE label for any non-default run."""
    lowered = chain.JANITOR_SYSTEM_PROMPT.lower()
    assert "pragmatic builder" not in lowered
    assert "pattern thinker" not in lowered
    # It should still point the curator at the role it's actually recapping.
    assert "agent role" in lowered


def test_janitor_prompt_keeps_indefinite_guardrail():
    """Neutralising the pairing must not disturb the loop-sacredness language."""
    lowered = chain.JANITOR_SYSTEM_PROMPT.lower()
    assert "indefinite" in lowered
    assert "no end state" in lowered


# --- bulletin dialogue headers ---

def _capture_bulletin_prompt(label_a, label_b):
    captured = {}

    def fake_call(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        return JanitorResult(success=True, working_set="BULLETIN:\nok")

    with patch("chain.call_janitor", side_effect=fake_call):
        chain.update_bulletin("dialogue A", "dialogue B", None,
                              label_a=label_a, label_b=label_b)
    return captured["prompt"]


def test_bulletin_default_labels_unchanged():
    """Default pairing keeps the exact BUILDER/THINKER DIALOGUE headers."""
    prompt = _capture_bulletin_prompt("Builder", "Thinker")
    assert "BUILDER DIALOGUE" in prompt
    assert "THINKER DIALOGUE" in prompt


def test_bulletin_custom_labels_carried_through():
    prompt = _capture_bulletin_prompt("Architect", "Contrarian")
    assert "ARCHITECT DIALOGUE" in prompt
    assert "CONTRARIAN DIALOGUE" in prompt
    assert "BUILDER DIALOGUE" not in prompt


# --- clear_and_recap forwards the labels to the curator recap call ---

def test_clear_and_recap_passes_role_labels_to_get_recap(tmp_path):
    """The label the curator is told to recap FOR must be the real role.

    Patches get_recap to capture the role_label argument for each side and
    return None, so the function bails right after the recap calls without
    touching tmux.
    """
    ja = tmp_path / "a.jsonl"
    jb = tmp_path / "b.jsonl"
    ja.write_text("", encoding="utf-8")
    jb.write_text("", encoding="utf-8")
    cur_a = JSONLCursor(file_path=ja)
    cur_b = JSONLCursor(file_path=jb)

    labels_seen = []

    def fake_get_recap(path, role_label, *a, **k):
        labels_seen.append(role_label)
        return None  # bail out before any /clear

    with patch("chain.get_recap", side_effect=fake_get_recap):
        chain.clear_and_recap(
            "sess:0.0", "sess:0.1", cur_a, cur_b,
            tmp_path / "log.md", 5,
            bulletin_path=None,
            label_a="Architect", label_b="Contrarian",
        )

    assert labels_seen == ["Architect", "Contrarian"]


def test_clear_and_recap_default_labels(tmp_path):
    """Default invocation still recaps as Builder / Thinker."""
    ja = tmp_path / "a.jsonl"
    jb = tmp_path / "b.jsonl"
    ja.write_text("", encoding="utf-8")
    jb.write_text("", encoding="utf-8")
    labels_seen = []

    with patch("chain.get_recap", side_effect=lambda p, rl, *a, **k: labels_seen.append(rl) or None):
        chain.clear_and_recap(
            "s:0.0", "s:0.1",
            JSONLCursor(file_path=ja), JSONLCursor(file_path=jb),
            tmp_path / "log.md", 5, bulletin_path=None,
        )
    assert labels_seen == ["Builder", "Thinker"]
