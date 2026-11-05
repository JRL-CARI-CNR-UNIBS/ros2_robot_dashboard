"""Launch arguments: parse ``ros2 launch ... --show-args`` and evaluate ``enabled_if``."""

from __future__ import annotations

import ast
import re
from typing import Any, Optional

from ..domain.process import ArgType, LaunchArg

_ARG_RE = re.compile(r"^\s{2,}'(?P<name>[^']+)':\s*$")
_DEFAULT_RE = re.compile(r"^\(default:\s*(?P<value>.*)\)\s*$")
_CHOICES_RE = re.compile(r"^(?:Valid choices are|Choices):\s*(?P<list>\[.*\])\s*$")


def parse_show_args(text: str) -> list[LaunchArg]:
    """Parse the output of ``ros2 launch <pkg> <file> --show-args``.

    Names, defaults, descriptions and choices are available; types are not, so a
    default of ``true``/``false`` becomes a bool and a choice list a choice.
    """
    args: list[LaunchArg] = []
    cur: Optional[dict] = None

    def flush() -> None:
        if cur is None:
            return
        default = cur.get('default')
        choices = tuple(cur.get('choices', ()))
        if choices:
            atype = ArgType.CHOICE
        elif isinstance(default, str) and default.lower() in ('true', 'false'):
            atype = ArgType.BOOL
            default = default.lower() == 'true'
        else:
            atype = ArgType.STRING
        args.append(LaunchArg(name=cur['name'], type=atype, default=default,
                              description=' '.join(cur['desc']).strip(), choices=choices,
                              required='default' not in cur))

    for line in text.splitlines():
        m = _ARG_RE.match(line)
        if m:
            flush()
            cur = {'name': m.group('name'), 'desc': []}
            continue
        if cur is None:
            continue
        s = line.strip()
        if not s:
            continue
        dm = _DEFAULT_RE.match(s)
        if dm:
            cur['default'] = _unquote(dm.group('value'))
            continue
        cm = _CHOICES_RE.match(s)
        if cm:
            try:
                cur['choices'] = [str(c) for c in ast.literal_eval(cm.group('list'))]
            except (ValueError, SyntaxError):
                pass
            continue
        if s.startswith('No description given'):
            continue
        cur['desc'].append(s)
    flush()
    return args


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
        return v[1:-1]
    return v


# --------------------------------------------------------------------------- conditions

_ALLOWED = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.Compare,
            ast.Eq, ast.NotEq, ast.In, ast.NotIn, ast.Name, ast.Load, ast.Constant, ast.List,
            ast.Tuple, ast.Lt, ast.LtE, ast.Gt, ast.GtE)


def evaluate_condition(expr: str, values: dict, profile: Any = None) -> bool:
    """Safely evaluate ``enabled_if`` (e.g. ``not sim``, ``controller == 'velocity'``).

    Only names of arguments, constants, ``not/and/or`` and comparisons are allowed.
    An invalid expression enables the argument (and is reported by ``check_condition``).
    """
    try:
        tree = ast.parse(expr.strip(), mode='eval')
    except SyntaxError:
        return True
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            return True
    env = {}
    for k, v in values.items():
        arg = profile.arg(k) if profile is not None and hasattr(profile, 'arg') else None
        if arg is not None and arg.type == ArgType.BOOL:
            env[k] = _as_bool(v)
        else:
            env[k] = v
    env.setdefault('true', True)
    env.setdefault('false', False)
    try:
        return bool(_eval(tree.body, env))
    except Exception:  # noqa: BLE001
        return True


def check_condition(expr: str, names: set) -> str:
    """Return an error message if the expression is invalid or uses unknown names."""
    try:
        tree = ast.parse(expr.strip(), mode='eval')
    except SyntaxError as exc:
        return f'syntax error: {exc.msg}'
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            return f'not allowed: {type(node).__name__}'
        if isinstance(node, ast.Name) and node.id not in names | {'true', 'false'}:
            return f"unknown argument '{node.id}'"
    return ''


def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ('true', '1', 'yes', 'on')


def _eval(node, env):
    if isinstance(node, ast.BoolOp):
        vals = [_eval(v, env) for v in node.values]
        return all(vals) if isinstance(node.op, ast.And) else any(vals)
    if isinstance(node, ast.UnaryOp):
        return not _eval(node.operand, env)
    if isinstance(node, ast.Compare):
        left = _eval(node.left, env)
        for op, comp in zip(node.ops, node.comparators):
            right = _eval(comp, env)
            ok = {
                ast.Eq: lambda a, b: _norm(a) == _norm(b),
                ast.NotEq: lambda a, b: _norm(a) != _norm(b),
                ast.In: lambda a, b: _norm(a) in [_norm(x) for x in b],
                ast.NotIn: lambda a, b: _norm(a) not in [_norm(x) for x in b],
                ast.Lt: lambda a, b: float(a) < float(b),
                ast.LtE: lambda a, b: float(a) <= float(b),
                ast.Gt: lambda a, b: float(a) > float(b),
                ast.GtE: lambda a, b: float(a) >= float(b),
            }[type(op)](left, right)
            if not ok:
                return False
            left = right
        return True
    if isinstance(node, ast.Name):
        if node.id not in env:
            raise NameError(node.id)
        return env[node.id]
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval(e, env) for e in node.elts]
    raise ValueError(type(node).__name__)


def _norm(v):
    if isinstance(v, bool):
        return 'true' if v else 'false'
    return str(v)
