"""Shared type definitions for the Janitor Engine."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class JanitorResult:
    """Output from a single janitor CLI call."""

    success: bool
    working_set: str = ""
    raw_response: str = ""
    error: Optional[str] = None
    duration_ms: int = 0
