"""Parameter presets in YAML compatible with ``ros2 param dump`` / ``ros2 param load``."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Any, Iterable

import yaml

from .domain.base import Serializable
from .domain.params import ParamInfo, ParamType, coerce_value, infer_type, values_equal


@dataclass(frozen=True)
class Diff(Serializable):
    name: str
    current: Any
    preset: Any
    missing: bool = False          # parameter in the preset but not on the node
    error: str = ''                # type mismatch or conversion error


def nest(flat: dict[str, Any]) -> dict:
    """``{'a.b.c': 1}`` -> ``{'a': {'b': {'c': 1}}}``."""
    root: dict = {}
    for name in sorted(flat):
        parts = name.split('.')
        cur = root
        ok = True
        for p in parts[:-1]:
            nxt = cur.setdefault(p, {})
            if not isinstance(nxt, dict):
                ok = False
                break
            cur = nxt
        if ok:
            if isinstance(cur.get(parts[-1]), dict):
                # A parameter and a namespace with the same name: keep the flat key.
                root[name] = flat[name]
            else:
                cur[parts[-1]] = flat[name]
        else:
            root[name] = flat[name]
    return root


def flatten(tree: dict, prefix: str = '') -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in (tree or {}).items():
        key = f'{prefix}.{k}' if prefix else str(k)
        if isinstance(v, dict):
            out.update(flatten(v, key))
        else:
            out[key] = v
    return out


def _yaml_value(info: ParamInfo) -> Any:
    v = info.value
    if info.type == ParamType.BYTE_ARRAY:
        return list(v or [])
    if isinstance(v, tuple):
        return list(v)
    return v


def dump_yaml(node_name: str, params: Iterable[ParamInfo], include_read_only: bool = False) -> str:
    flat = {}
    for p in params:
        if p.type == ParamType.NOT_SET:
            continue
        if p.descriptor.read_only and not include_read_only:
            continue
        flat[p.name] = _yaml_value(p)
    doc = {node_name: {'ros__parameters': nest(flat)}}
    return yaml.safe_dump(doc, default_flow_style=None, sort_keys=True, allow_unicode=True)


def save(path: str, node_name: str, params: Iterable[ParamInfo],
         include_read_only: bool = False) -> None:
    with open(path, 'w', encoding='utf-8') as f:
        f.write(dump_yaml(node_name, params, include_read_only))


def parse_yaml(text: str) -> dict[str, dict[str, Any]]:
    """Return ``{node_name_or_pattern: {dotted.param: value}}``."""
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError('preset: top level must be a mapping')
    out: dict[str, dict[str, Any]] = {}

    def visit(prefix: str, d: dict) -> None:
        for key, val in d.items():
            if not isinstance(val, dict):
                continue
            if 'ros__parameters' in val:
                name = _join_node(prefix, str(key))
                out.setdefault(name, {}).update(flatten(val['ros__parameters'] or {}))
            else:
                # Namespaced form: {ns: {node: {ros__parameters: ...}}}
                visit(_join_node(prefix, str(key)), val)

    visit('', data)
    return out


def _join_node(prefix: str, key: str) -> str:
    if key.startswith('/') or key == '/**' or prefix == '':
        name = key
    else:
        name = prefix.rstrip('/') + '/' + key
    if not name.startswith('/') and not name.startswith('*'):
        name = '/' + name
    return name


def load(path: str) -> dict[str, dict[str, Any]]:
    with open(path, encoding='utf-8') as f:
        return parse_yaml(f.read())


def values_for_node(preset: dict[str, dict[str, Any]], node_name: str) -> dict[str, Any]:
    """Merge wildcard (``/**``, glob) and exact entries for one node; exact wins."""
    merged: dict[str, Any] = {}
    exact = None
    for key, values in preset.items():
        if key == node_name or key.lstrip('/') == node_name.lstrip('/'):
            exact = values
        elif key in ('/**', '**') or fnmatch.fnmatchcase(node_name, key.replace('/**', '/*')):
            merged.update(values)
    if exact:
        merged.update(exact)
    return merged


def diff(current: Iterable[ParamInfo], preset_values: dict[str, Any]) -> list[Diff]:
    """Differences between the node parameters and a preset.

    Integers are promoted to doubles; the opposite is an error (design v1, 5.8).
    """
    by_name = {p.name: p for p in current}
    out: list[Diff] = []
    for name, raw in sorted(preset_values.items()):
        info = by_name.get(name)
        if info is None:
            out.append(Diff(name, None, raw, missing=True))
            continue
        target_type = info.type
        if target_type == ParamType.NOT_SET or info.descriptor.dynamic_typing:
            target_type = infer_type(raw) if info.type == ParamType.NOT_SET else info.type
        try:
            if target_type == ParamType.INTEGER and isinstance(raw, float) and not raw.is_integer():
                raise ValueError(f'{raw} is a double, parameter is an integer')
            value = coerce_value(raw, target_type)
        except ValueError as exc:
            out.append(Diff(name, info.value, raw, error=str(exc)))
            continue
        if not values_equal(value, info.value):
            out.append(Diff(name, info.value, value))
    return out
