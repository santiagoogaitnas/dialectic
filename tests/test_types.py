"""Tests for janitor.types — verify all dataclasses can be instantiated."""

from janitor.types import JanitorResult


def test_janitor_result_defaults():
    r = JanitorResult(success=True)
    assert r.success is True
    assert r.working_set == ""
    assert r.raw_response == ""
    assert r.error is None
    assert r.duration_ms == 0


def test_janitor_result_full():
    r = JanitorResult(
        success=False,
        working_set="ws",
        raw_response="raw",
        error="oops",
        duration_ms=100,
    )
    assert r.success is False
    assert r.error == "oops"
