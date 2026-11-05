"""Base helpers for domain types: JSON-friendly serialization."""

from __future__ import annotations

import dataclasses
import enum
import math
from typing import Any


def to_jsonable(value: Any) -> Any:
    """Convert dataclasses, enums, tuples and bytes into JSON-friendly values."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, enum.Enum):
        return value.name
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, (bytes, bytearray)):
        return list(value)
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


class Serializable:
    """Mixin for dataclasses: ``to_dict()`` returns a JSON-serializable dict."""

    def to_dict(self) -> dict:
        return to_jsonable(self)
