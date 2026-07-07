"""Regression guard for the directory-picker click wiring.

The picker once built its rows as
``onclick="loadPickerPath(${JSON.stringify(entry.path)})"``. Because
JSON.stringify wraps the path in double quotes and the HTML attribute is
also double-quoted, the browser closed the attribute at the first quote of
the path — every row's handler collapsed to the dead fragment
``loadPickerPath(`` and nothing was clickable.

There's no JS test runner in this repo (stdlib-only, no node), so the
behavioural proof is a live browser check. These tests are a cheap static
backstop: they assert the source never regresses to interpolating a path
into an inline on* handler, and that rows still carry the index-based
``data-idx`` hooks the delegated listeners depend on.
"""

from pathlib import Path

import pytest

INDEX_HTML = Path(__file__).parent.parent / "ui" / "static" / "index.html"


@pytest.fixture(scope="module")
def html() -> str:
    if not INDEX_HTML.exists():
        pytest.skip("ui/static/index.html not present")
    return INDEX_HTML.read_text(encoding="utf-8")


def test_no_path_interpolated_into_inline_handlers(html):
    """The exact shape of the original bug must not reappear.

    A path (or any JSON.stringify result) placed inside a double-quoted
    on* attribute breaks attribute parsing. Guard the two call sites that
    carried directory paths.
    """
    assert 'onclick="loadPickerPath(${' not in html
    assert 'ondblclick="pickEntry(${' not in html
    assert 'onclick="loadPickerPath(${JSON.stringify' not in html


def test_picker_rows_use_index_dataset(html):
    """Rows/suggestions carry integer indices the delegated handlers read."""
    assert 'data-idx="${i}"' in html
    assert 'data-sidx="${i}"' in html


def test_picker_has_delegated_listeners(html):
    """Navigation is wired via a delegated listener, not per-row inline JS."""
    assert "ensurePickerDelegation" in html
    assert "_pickerEntryPath" in html
