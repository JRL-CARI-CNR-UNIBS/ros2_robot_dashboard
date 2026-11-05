"""Parameter types and conversions (design v1, section 5.1).

The conversion helpers import ``rcl_interfaces`` lazily so the pure parts of this
module (types, parsing, validation) can be used and tested without ROS.
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass, field
from typing import Any, Generic, Optional, TypeVar

from .base import Serializable

T = TypeVar('T', int, float)


class ParamType(enum.IntEnum):
    """Mirror of ``rcl_interfaces/msg/ParameterType`` (values 0-9)."""

    NOT_SET = 0
    BOOL = 1
    INTEGER = 2
    DOUBLE = 3
    STRING = 4
    BYTE_ARRAY = 5
    BOOL_ARRAY = 6
    INTEGER_ARRAY = 7
    DOUBLE_ARRAY = 8
    STRING_ARRAY = 9

    @property
    def is_array(self) -> bool:
        return self in ARRAY_TYPES

    @property
    def label(self) -> str:
        return self.name.lower().replace('_', ' ')


ARRAY_TYPES = frozenset({
    ParamType.BYTE_ARRAY, ParamType.BOOL_ARRAY, ParamType.INTEGER_ARRAY,
    ParamType.DOUBLE_ARRAY, ParamType.STRING_ARRAY,
})

_ELEMENT_TYPE = {
    ParamType.BOOL_ARRAY: ParamType.BOOL,
    ParamType.INTEGER_ARRAY: ParamType.INTEGER,
    ParamType.DOUBLE_ARRAY: ParamType.DOUBLE,
    ParamType.STRING_ARRAY: ParamType.STRING,
    ParamType.BYTE_ARRAY: ParamType.INTEGER,
}

# Field of ParameterValue holding the value for each type.
_VALUE_FIELD = {
    ParamType.BOOL: 'bool_value',
    ParamType.INTEGER: 'integer_value',
    ParamType.DOUBLE: 'double_value',
    ParamType.STRING: 'string_value',
    ParamType.BYTE_ARRAY: 'byte_array_value',
    ParamType.BOOL_ARRAY: 'bool_array_value',
    ParamType.INTEGER_ARRAY: 'integer_array_value',
    ParamType.DOUBLE_ARRAY: 'double_array_value',
    ParamType.STRING_ARRAY: 'string_array_value',
}


class ParamStatus(enum.Enum):
    OK = 'ok'
    MODIFIED = 'modified'
    PENDING = 'pending'
    REJECTED = 'rejected'
    EXTERNAL_CHANGE = 'external_change'
    STALE = 'stale'


class NodeStatus(enum.Enum):
    DISCOVERED = 'discovered'
    LOADING = 'loading'
    READY = 'ready'
    UNREACHABLE = 'unreachable'
    GONE = 'gone'


@dataclass(frozen=True)
class Range(Serializable, Generic[T]):
    from_value: T
    to_value: T
    step: T


@dataclass(frozen=True)
class ParamDescriptorInfo(Serializable):
    description: str = ''
    read_only: bool = False
    dynamic_typing: bool = False
    float_range: Optional[Range] = None
    int_range: Optional[Range] = None
    additional_constraints: str = ''


@dataclass(frozen=True)
class ParamInfo(Serializable):
    name: str
    type: ParamType
    value: Any
    descriptor: ParamDescriptorInfo = field(default_factory=ParamDescriptorInfo)


@dataclass(frozen=True)
class SetResult(Serializable):
    name: str
    successful: bool
    reason: str = ''


# --------------------------------------------------------------------------- ROS conversions

def param_type_from_msg(value_msg) -> ParamType:
    try:
        return ParamType(int(value_msg.type))
    except ValueError:
        return ParamType.NOT_SET


def value_from_msg(value_msg) -> Any:
    """Extract the Python value from a ``ParameterValue`` message."""
    ptype = param_type_from_msg(value_msg)
    if ptype == ParamType.NOT_SET:
        return None
    raw = getattr(value_msg, _VALUE_FIELD[ptype])
    if ptype == ParamType.BYTE_ARRAY:
        return [b if isinstance(b, int) else ord(b) for b in raw]
    if ptype.is_array:
        return list(raw)
    return raw


def value_to_msg(value: Any, ptype: ParamType):
    """Build a ``ParameterValue``; raises ``ValueError`` if not convertible."""
    from rcl_interfaces.msg import ParameterValue

    msg = ParameterValue()
    msg.type = int(ptype)
    if ptype == ParamType.NOT_SET:
        return msg
    value = coerce_value(value, ptype)
    if ptype == ParamType.BYTE_ARRAY:
        value = [bytes([v]) for v in value]
    setattr(msg, _VALUE_FIELD[ptype], value)
    return msg


def descriptor_from_msg(desc_msg) -> ParamDescriptorInfo:
    float_range = None
    int_range = None
    if len(desc_msg.floating_point_range) > 0:
        r = desc_msg.floating_point_range[0]
        float_range = Range(float(r.from_value), float(r.to_value), float(r.step))
    if len(desc_msg.integer_range) > 0:
        r = desc_msg.integer_range[0]
        int_range = Range(int(r.from_value), int(r.to_value), int(r.step))
    return ParamDescriptorInfo(
        description=desc_msg.description,
        read_only=bool(desc_msg.read_only),
        dynamic_typing=bool(getattr(desc_msg, 'dynamic_typing', False)),
        float_range=float_range,
        int_range=int_range,
        additional_constraints=desc_msg.additional_constraints,
    )


# --------------------------------------------------------------------------- parsing and validation

_TRUE = {'true', '1', 'yes', 'on', 'y'}
_FALSE = {'false', '0', 'no', 'off', 'n'}


def _parse_bool(text: Any) -> bool:
    if isinstance(text, bool):
        return text
    if isinstance(text, (int, float)) and text in (0, 1):
        return bool(text)
    s = str(text).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    raise ValueError(f"'{text}' is not a bool")


def _parse_int(text: Any) -> int:
    if isinstance(text, bool):
        raise ValueError(f"'{text}' is not an integer")
    if isinstance(text, int):
        return text
    if isinstance(text, float):
        if text.is_integer():
            return int(text)
        raise ValueError(f"'{text}' is not an integer")
    s = str(text).strip()
    try:
        return int(s, 0)
    except ValueError:
        raise ValueError(f"'{text}' is not an integer") from None


def _parse_float(text: Any) -> float:
    if isinstance(text, bool):
        raise ValueError(f"'{text}' is not a double")
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).strip().lower()
    if s in ('inf', '+inf', '.inf'):
        return math.inf
    if s in ('-inf', '-.inf'):
        return -math.inf
    if s in ('nan', '.nan'):
        return math.nan
    try:
        return float(s)
    except ValueError:
        raise ValueError(f"'{text}' is not a double") from None


_SCALAR_PARSERS = {
    ParamType.BOOL: _parse_bool,
    ParamType.INTEGER: _parse_int,
    ParamType.DOUBLE: _parse_float,
    ParamType.STRING: lambda v: v if isinstance(v, str) else str(v),
}


def _split_array_text(text: str) -> list[str]:
    s = text.strip()
    if s.startswith('[') and s.endswith(']'):
        s = s[1:-1]
    if not s.strip():
        return []
    # Respect simple quoting for string arrays: "a, b", 'c'
    items, buf, quote = [], [], None
    for ch in s:
        if quote:
            if ch == quote:
                quote = None
            else:
                buf.append(ch)
        elif ch in ('"', "'"):
            quote = ch
        elif ch == ',':
            items.append(''.join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    items.append(''.join(buf).strip())
    return items


def coerce_value(value: Any, ptype: ParamType) -> Any:
    """Convert a Python value (or text) to the canonical value for ``ptype``.

    Array errors point at the faulty element (design v1, robustness rule 7).
    """
    if ptype == ParamType.NOT_SET:
        return None
    if ptype.is_array:
        if isinstance(value, str):
            items = _split_array_text(value)
        elif isinstance(value, (bytes, bytearray)):
            items = list(value)
        else:
            try:
                items = list(value)
            except TypeError:
                raise ValueError(f"'{value}' is not an array") from None
        elem = _ELEMENT_TYPE[ptype]
        out = []
        for i, item in enumerate(items):
            try:
                out.append(_SCALAR_PARSERS[elem](item))
            except ValueError as exc:
                raise ValueError(f'element {i}: {exc}') from None
        if ptype == ParamType.BYTE_ARRAY:
            for i, b in enumerate(out):
                if not 0 <= b <= 255:
                    raise ValueError(f'element {i}: {b} is not a byte (0-255)')
        return out
    return _SCALAR_PARSERS[ptype](value)


def coerce_text(text: str, ptype: ParamType) -> Any:
    """Parse text typed by the user (``1, 2, 3``, ``true``...)."""
    return coerce_value(text, ptype)


def infer_type(value: Any) -> ParamType:
    """Guess the parameter type of a Python value (used for dynamic typing and presets)."""
    if value is None:
        return ParamType.NOT_SET
    if isinstance(value, bool):
        return ParamType.BOOL
    if isinstance(value, int):
        return ParamType.INTEGER
    if isinstance(value, float):
        return ParamType.DOUBLE
    if isinstance(value, str):
        return ParamType.STRING
    if isinstance(value, (bytes, bytearray)):
        return ParamType.BYTE_ARRAY
    if isinstance(value, (list, tuple)):
        if not value:
            return ParamType.STRING_ARRAY
        if all(isinstance(v, bool) for v in value):
            return ParamType.BOOL_ARRAY
        if all(isinstance(v, int) and not isinstance(v, bool) for v in value):
            return ParamType.INTEGER_ARRAY
        if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
            return ParamType.DOUBLE_ARRAY
        return ParamType.STRING_ARRAY
    return ParamType.STRING


def validate(value: Any, info: ParamInfo) -> tuple[bool, str]:
    """Local type and range check. It does not replace validation by the node."""
    if info.descriptor.read_only:
        return False, 'parameter is read-only'
    ptype = info.type
    if info.descriptor.dynamic_typing and ptype == ParamType.NOT_SET:
        ptype = infer_type(value)
    try:
        v = coerce_value(value, ptype)
    except ValueError as exc:
        return False, str(exc)
    d = info.descriptor
    if ptype == ParamType.INTEGER and d.int_range is not None:
        r = d.int_range
        if not r.from_value <= v <= r.to_value:
            return False, f'{v} outside range [{r.from_value}, {r.to_value}]'
        if r.step and (v - r.from_value) % r.step != 0 and v != r.to_value:
            return False, f'{v} is not a multiple of step {r.step} from {r.from_value}'
    if ptype == ParamType.DOUBLE and d.float_range is not None:
        r = d.float_range
        tol = 1e-9 * max(1.0, abs(r.to_value - r.from_value))
        if not (r.from_value - tol) <= v <= (r.to_value + tol):
            return False, f'{v} outside range [{r.from_value}, {r.to_value}]'
        if r.step:
            k = (v - r.from_value) / r.step
            if abs(k - round(k)) > 1e-6 and abs(v - r.to_value) > tol:
                return False, f'{v} is not on step {r.step} from {r.from_value}'
    return True, ''


def describe_range(descriptor: ParamDescriptorInfo) -> str:
    if descriptor.int_range is not None:
        r = descriptor.int_range
        step = f', step {r.step}' if r.step else ''
        return f'[{r.from_value}, {r.to_value}{step}]'
    if descriptor.float_range is not None:
        r = descriptor.float_range
        step = f', step {r.step:g}' if r.step else ''
        return f'[{r.from_value:g}, {r.to_value:g}{step}]'
    return ''


def format_value(value: Any, ptype: ParamType) -> str:
    """Compact text for a value (used by tables and the text editor)."""
    if value is None:
        return ''
    if ptype == ParamType.BOOL:
        return 'true' if value else 'false'
    if ptype.is_array:
        if ptype == ParamType.STRING_ARRAY:
            return '[' + ', '.join(_quote_if_needed(s) for s in value) + ']'
        if ptype == ParamType.BOOL_ARRAY:
            return '[' + ', '.join('true' if v else 'false' for v in value) + ']'
        return '[' + ', '.join(_fmt_num(v) for v in value) + ']'
    if ptype == ParamType.DOUBLE:
        return _fmt_num(value)
    return str(value)


def _fmt_num(v: Any) -> str:
    if isinstance(v, float):
        return repr(v)
    return str(v)


def _quote_if_needed(s: str) -> str:
    if s == '' or any(c in s for c in ',[]"\'') or s != s.strip():
        return '"' + s.replace('"', "'") + '"'
    return s


def values_equal(a: Any, b: Any) -> bool:
    """Equality that treats NaN == NaN and compares sequences elementwise."""
    if isinstance(a, float) and isinstance(b, float):
        return a == b or (math.isnan(a) and math.isnan(b))
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(values_equal(x, y) for x, y in zip(a, b))
    return a == b
