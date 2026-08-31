"""Validation for identifiers used in local runtime artifact paths."""

from __future__ import annotations

import re

_SAFE_RUNTIME_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def validate_runtime_id(value: str, *, label: str) -> str:
    """Return a path-safe identifier or raise a field-specific error."""

    if not _SAFE_RUNTIME_ID.fullmatch(value) or not any(character != "." for character in value):
        raise ValueError(f"{label} must be a safe non-empty identifier")
    return value
