"""Message -> YAML text, like ``ros2 topic echo``, with array truncation."""

from __future__ import annotations

from typing import Optional


def format_message(msg, compat, truncate_length: Optional[int] = None,
                   no_arr: bool = False) -> str:
    """YAML text of ``msg``. Long arrays/strings are cut by ``truncate_length``."""
    try:
        text = compat.message_to_yaml(msg, truncate_length=truncate_length, no_arr=no_arr)
    except Exception as exc:  # noqa: BLE001 - never break the echo for one message
        return f'# cannot format message: {exc}\n'
    return text


def metadata_only(msg, compat) -> str:
    """Short view for heavy messages: header and scalar fields, sizes of sequences."""
    lines = []
    fields = msg.get_fields_and_field_types()
    for name in fields:
        value = getattr(msg, name)
        if hasattr(value, 'get_fields_and_field_types'):
            if name == 'header':
                sub = compat.message_to_yaml(value).rstrip().splitlines()
                lines.append('header:')
                lines += ['  ' + s for s in sub]
            else:
                lines.append(f'{name}: <{type(value).__name__}>')
        elif isinstance(value, (bytes, bytearray, list, tuple)) or hasattr(value, '__len__') \
                and not isinstance(value, str):
            try:
                lines.append(f'{name}: <{len(value)} elements>')
            except TypeError:
                lines.append(f'{name}: {value!r}')
        else:
            lines.append(f'{name}: {value!r}' if isinstance(value, str) else f'{name}: {value}')
    return '\n'.join(lines) + '\n'
