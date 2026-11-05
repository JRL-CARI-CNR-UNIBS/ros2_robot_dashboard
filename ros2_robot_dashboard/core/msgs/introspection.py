"""Dynamic message/service/action types: import by name, field schema, field access."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

NUMERIC_TYPES = {
    'float', 'double', 'long double', 'float32', 'float64',
    'int8', 'uint8', 'int16', 'uint16', 'int32', 'uint32', 'int64', 'uint64',
    'char', 'octet', 'byte', 'boolean', 'bool',
}
INTEGER_TYPES = {'int8', 'uint8', 'int16', 'uint16', 'int32', 'uint32', 'int64', 'uint64',
                 'char', 'octet', 'byte'}


class TypeUnavailable(LookupError):
    """The interface type cannot be imported on this machine."""


def normalize_type(type_str: str, kind: str = 'msg') -> str:
    """``pkg/Name`` -> ``pkg/<kind>/Name``; full names are returned unchanged."""
    parts = type_str.strip().split('/')
    if len(parts) == 2:
        return f'{parts[0]}/{kind}/{parts[1]}'
    return type_str.strip()


def get_message_class(type_str: str):
    from rosidl_runtime_py.utilities import get_message
    try:
        return get_message(normalize_type(type_str, 'msg'))
    except (AttributeError, ModuleNotFoundError, ValueError, ImportError) as exc:
        raise TypeUnavailable(f"message type '{type_str}' not available on this PC: {exc}") \
            from None


def get_service_class(type_str: str):
    from rosidl_runtime_py.utilities import get_service
    try:
        return get_service(normalize_type(type_str, 'srv'))
    except (AttributeError, ModuleNotFoundError, ValueError, ImportError) as exc:
        raise TypeUnavailable(f"service type '{type_str}' not available on this PC: {exc}") \
            from None


def get_action_class(type_str: str):
    from rosidl_runtime_py.utilities import get_action
    try:
        return get_action(normalize_type(type_str, 'action'))
    except (AttributeError, ModuleNotFoundError, ValueError, ImportError) as exc:
        raise TypeUnavailable(f"action type '{type_str}' not available on this PC: {exc}") \
            from None


@dataclass
class FieldSpec:
    name: str
    type_name: str               # e.g. 'float64', 'string', 'geometry_msgs/msg/Point'
    is_sequence: bool = False
    is_fixed_array: bool = False
    size: Optional[int] = None   # fixed size or upper bound
    is_message: bool = False
    children: list['FieldSpec'] = field(default_factory=list)

    @property
    def is_numeric(self) -> bool:
        return not self.is_message and self.type_name in NUMERIC_TYPES

    @property
    def is_string(self) -> bool:
        return self.type_name in ('string', 'wstring')

    def label(self) -> str:
        t = self.type_name
        if self.is_fixed_array:
            return f'{t}[{self.size}]'
        if self.is_sequence:
            return f'{t}[<={self.size}]' if self.size else f'{t}[]'
        return t


def _slot_to_spec(name: str, slot, depth: int, max_depth: int) -> FieldSpec:
    from rosidl_parser import definition as d

    spec = FieldSpec(name, '')
    t = slot
    if isinstance(t, d.Array):
        spec.is_fixed_array, spec.size, t = True, t.size, t.value_type
    elif isinstance(t, d.BoundedSequence):
        spec.is_sequence, spec.size, t = True, t.maximum_size, t.value_type
    elif isinstance(t, d.UnboundedSequence):
        spec.is_sequence, t = True, t.value_type
    if isinstance(t, d.NamespacedType):
        spec.type_name = '/'.join(t.namespaces + [t.name])
        spec.is_message = True
        if depth < max_depth:
            try:
                cls = get_message_class(spec.type_name)
                spec.children = message_schema(cls, depth + 1, max_depth)
            except TypeUnavailable:
                pass
    elif isinstance(t, (d.AbstractString, d.AbstractWString)):
        spec.type_name = 'wstring' if isinstance(t, d.AbstractWString) else 'string'
    elif isinstance(t, d.BasicType):
        spec.type_name = t.typename
    else:
        spec.type_name = str(t)
    return spec


def message_schema(msg_cls, depth: int = 0, max_depth: int = 8) -> list[FieldSpec]:
    """Field tree of a message class (from ``SLOT_TYPES``)."""
    names = list(msg_cls.get_fields_and_field_types().keys())
    slots = list(getattr(msg_cls, 'SLOT_TYPES', ()))
    return [_slot_to_spec(n, s, depth, max_depth) for n, s in zip(names, slots)]


def numeric_paths(msg_cls, max_depth: int = 6, max_paths: int = 400) -> list[str]:
    """Paths of numeric fields, for the plot picker (``pose.position.x``, ``position[0]``)."""
    out: list[str] = []

    def walk(specs: list[FieldSpec], prefix: str) -> None:
        for s in specs:
            if len(out) >= max_paths:
                return
            path = f'{prefix}.{s.name}' if prefix else s.name
            if s.is_message:
                if s.is_sequence or s.is_fixed_array:
                    walk(s.children, f'{path}[0]')
                else:
                    walk(s.children, path)
            elif s.is_numeric:
                if s.is_fixed_array:
                    out.extend(f'{path}[{i}]' for i in range(min(s.size or 0, 16)))
                elif s.is_sequence:
                    out.append(f'{path}[0]')
                else:
                    out.append(path)

    walk(message_schema(msg_cls, 0, max_depth), '')
    return out


_PATH_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)((?:\[\d+\])*)')


def parse_path(path: str) -> list:
    """``a.b[2].c`` -> ``['a', 'b', 2, 'c']``."""
    parts: list = []
    for chunk in path.strip().strip('.').split('.'):
        m = _PATH_RE.fullmatch(chunk)
        if not m:
            raise ValueError(f"invalid field path '{path}'")
        parts.append(m.group(1))
        for idx in re.findall(r'\[(\d+)\]', m.group(2)):
            parts.append(int(idx))
    return parts


def extract_field(msg, path: str | list) -> Any:
    parts = parse_path(path) if isinstance(path, str) else path
    cur = msg
    for p in parts:
        if isinstance(p, int):
            cur = cur[p]
        else:
            cur = getattr(cur, p)
    return cur


def to_float(value: Any) -> float:
    if isinstance(value, (bytes, bytearray)):
        return float(value[0]) if value else 0.0
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    return float(value)


def header_stamp(msg) -> Optional[float]:
    """``header.stamp`` in seconds, or None when the message has no header."""
    header = getattr(msg, 'header', None)
    stamp = getattr(header, 'stamp', None) if header is not None else None
    if stamp is None:
        return None
    try:
        t = stamp.sec + stamp.nanosec * 1e-9
    except AttributeError:
        return None
    return t if t > 0 else None


def default_request_dict(srv_or_msg_cls) -> dict:
    from rosidl_runtime_py import message_to_ordereddict
    return dict(message_to_ordereddict(srv_or_msg_cls()))


def fill_message(msg_cls, values: dict):
    """Instantiate ``msg_cls`` and set fields from a (nested) dict."""
    from rosidl_runtime_py import set_message_fields
    msg = msg_cls()
    if values:
        try:
            set_message_fields(msg, values)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f'cannot set fields: {exc}') from None
    return msg


def message_to_dict(msg) -> dict:
    from rosidl_runtime_py import message_to_ordereddict
    return _plain(message_to_ordereddict(msg))


def _plain(v):
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    return v
