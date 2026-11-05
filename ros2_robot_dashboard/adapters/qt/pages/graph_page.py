"""Nodes and graph page (alternative to rqt_graph).

- Layout with Graphviz ``dot`` in a worker thread; recomputed only when the filtered
  topology changes (or on request), so the picture stays stable.
- Positions moved by hand are remembered (also across sessions).
- Filters, neighbours-only mode, QoS-coloured edges, export to PNG/SVG/DOT.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
from collections import deque
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QFont, QImage, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import (QCheckBox, QFileDialog, QGraphicsItem, QGraphicsPathItem,
                               QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel, QPlainTextEdit,
                               QSpinBox, QSplitter, QVBoxLayout)

from ....core.config import state_dir
from ....core.domain.events import GraphChanged
from ....core.domain.graph import GraphSnapshot, qos_issues
from .. import theme
from ..widgets.common import FilterEdit, button
from .base_page import BasePage

DEBUG_TOPICS = {'/rosout', '/parameter_events', '/tf', '/tf_static', '/clock'}
SCALE = 72.0          # graphviz inches -> scene points


# --------------------------------------------------------------------------- graph model

class GraphView:
    """Filtered bipartite graph: ROS nodes and topics, edges pub (n->t) and sub (t->n)."""

    def __init__(self):
        self.nodes: dict[str, str] = {}     # key -> label ; key 'n:/name' or 't:/topic'
        self.edges: list[tuple[str, str, bool]] = []    # (src, dst, qos_problem)
        self.truncated = False

    def key(self) -> tuple:
        return (tuple(sorted(self.nodes)), tuple(sorted((a, b) for a, b, _ in self.edges)))


def build_view(snap: GraphSnapshot, hide_debug: bool, hide_dead: bool, hidden_node,
               ns_filter: str, text: str, focus: str, depth: int, max_elements: int,
               hide_isolated: bool = False) -> GraphView:
    v = GraphView()
    nodes = {n.fqn for n in snap.nodes if not hidden_node(n.fqn)}
    edges = []
    topic_names = {}
    for t in snap.topics:
        if hide_debug and t.name in DEBUG_TOPICS:
            continue
        pubs = [p.node_fqn for p in t.publishers if p.node_fqn in nodes]
        subs = [s.node_fqn for s in t.subscribers if s.node_fqn in nodes]
        if not pubs and not subs:
            continue
        if hide_dead and (not pubs or not subs):
            continue
        bad = {(i.publisher, i.subscriber) for i in qos_issues(t)}
        tk = 't:' + t.name
        topic_names[tk] = t.name
        for p in pubs:
            edges.append(('n:' + p, tk, any(b[0] == p for b in bad)))
        for s in subs:
            edges.append((tk, 'n:' + s, any(b[1] == s for b in bad)))
    keys = {'n:' + n for n in nodes} | set(topic_names)
    if ns_filter:
        ns = ns_filter if ns_filter.startswith('/') else '/' + ns_filter
        keys = {k for k in keys if k[2:].startswith(ns)}
    if text:
        q = text.lower()
        match = {k for k in keys if q in k[2:].lower()}
        # keep matching items and their direct connections
        keep = set(match)
        for a, b, _ in edges:
            if a in match:
                keep.add(b)
            if b in match:
                keep.add(a)
        keys &= keep
    if focus and focus in keys:
        adj: dict[str, set] = {}
        for a, b, _ in edges:
            adj.setdefault(a, set()).add(b)
            adj.setdefault(b, set()).add(a)
        seen = {focus}
        frontier = deque([(focus, 0)])
        # depth counts node-to-node hops (each hop crosses a topic)
        while frontier:
            k, d = frontier.popleft()
            if d >= depth * 2:
                continue
            for nb in adj.get(k, ()):
                if nb not in seen:
                    seen.add(nb)
                    frontier.append((nb, d + 1))
        keys &= seen
    if hide_isolated:
        connected = {a for a, b, _ in edges if a in keys and b in keys} | \
            {b for a, b, _ in edges if a in keys and b in keys}
        keys = {k for k in keys if k in connected or k == focus}
    if len(keys) > max_elements:
        v.truncated = True
        keys = set(sorted(keys)[:max_elements])
    for k in keys:
        v.nodes[k] = k[2:]
    v.edges = [(a, b, q) for a, b, q in edges if a in keys and b in keys]
    return v


def to_dot(v: GraphView) -> str:
    ids = {k: f'x{i}' for i, k in enumerate(sorted(v.nodes))}
    lines = ['digraph G {', '  rankdir=LR; nodesep=0.25; ranksep=0.9;',
             '  node [fontsize=10, fontname="Helvetica"];']
    for k, label in sorted(v.nodes.items()):
        shape = 'ellipse' if k.startswith('n:') else 'box'
        lines.append(f'  {ids[k]} [label="{_esc(label)}", shape={shape}];')
    for a, b, q in v.edges:
        lines.append(f'  {ids[a]} -> {ids[b]}' + (' [color=red]' if q else '') + ';')
    lines.append('}')
    return '\n'.join(lines)


def export_dot(v: GraphView) -> str:
    """DOT with readable ids (for export)."""
    lines = ['digraph ros_graph {', '  rankdir=LR;']
    for k, label in sorted(v.nodes.items()):
        shape = 'ellipse' if k.startswith('n:') else 'box'
        lines.append(f'  "{_esc(k)}" [label="{_esc(label)}", shape={shape}];')
    for a, b, q in v.edges:
        lines.append(f'  "{_esc(a)}" -> "{_esc(b)}"' + (' [color=red]' if q else '') + ';')
    lines.append('}')
    return '\n'.join(lines)


def _esc(s: str) -> str:
    return s.replace('\\', '\\\\').replace('"', '\\"')


def layout_with_dot(v: GraphView) -> dict[str, tuple[float, float]]:
    """Run ``dot -Tplain`` and return scene positions; fall back to a layered layout."""
    exe = shutil.which('dot')
    if exe is None:
        return layered_layout(v)
    ids = {f'x{i}': k for i, k in enumerate(sorted(v.nodes))}
    res = subprocess.run([exe, '-Tplain'], input=to_dot(v), capture_output=True, text=True,
                         timeout=30)
    if res.returncode != 0:
        return layered_layout(v)
    pos = {}
    height = 0.0
    for line in res.stdout.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == 'graph':
            height = float(parts[3])
        elif parts[0] == 'node':
            k = ids.get(parts[1])
            if k is not None:
                pos[k] = (float(parts[2]) * SCALE, (height - float(parts[3])) * SCALE)
    for k in v.nodes:
        pos.setdefault(k, (0.0, 0.0))
    return pos


def layered_layout(v: GraphView) -> dict[str, tuple[float, float]]:
    rank = {k: 0 for k in v.nodes}
    for _ in range(len(v.nodes)):
        changed = False
        for a, b, _q in v.edges:
            if rank[b] < rank[a] + 1 and rank[a] + 1 < len(v.nodes):
                rank[b] = rank[a] + 1
                changed = True
        if not changed:
            break
    cols: dict[int, list[str]] = {}
    for k in sorted(v.nodes):
        cols.setdefault(rank[k], []).append(k)
    return {k: (r * 220.0, i * 60.0) for r, ks in cols.items() for i, k in enumerate(ks)}


# --------------------------------------------------------------------------- scene items

class _Item:
    key: str
    edges: list


class NodeShape(QGraphicsItem):
    def __init__(self, key: str, label: str, is_node: bool, on_moved):
        super().__init__()
        self.key = key
        self.label = label
        self.is_node = is_node
        self.edges: list['EdgeShape'] = []
        self._on_moved = on_moved
        self._font = QFont()
        self._font.setPointSize(9)
        from PySide6.QtGui import QFontMetricsF
        fm = QFontMetricsF(self._font)
        w = fm.horizontalAdvance(label) + (26 if is_node else 16)
        h = fm.height() + 12
        self._rect = QRectF(-w / 2, -h / 2, w, h)
        self.highlight = False
        self.dim = False
        self.setFlags(QGraphicsItem.ItemIsMovable | QGraphicsItem.ItemIsSelectable
                      | QGraphicsItem.ItemSendsGeometryChanges)
        self.setZValue(1)
        self.setToolTip(label)

    def boundingRect(self) -> QRectF:
        return self._rect.adjusted(-2, -2, 2, 2)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        dark = theme.current() == 'dark'
        if self.is_node:
            fill = QColor('#2d4a73') if dark else QColor('#dce9fb')
            border = QColor('#6ea0e6') if dark else QColor('#3a7bd5')
        else:
            fill = QColor('#4a4a2d') if dark else QColor('#fdf6dc')
            border = QColor('#c9b458') if dark else QColor('#b8a030')
        if self.isSelected() or self.highlight:
            border = QColor(theme.COLORS['error'] if self.isSelected() else '#e67e22')
        if self.dim:
            fill.setAlpha(80)
            border.setAlpha(80)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(border, 2 if (self.isSelected() or self.highlight) else 1.2))
        painter.setBrush(QBrush(fill))
        if self.is_node:
            painter.drawEllipse(self._rect)
        else:
            painter.drawRoundedRect(self._rect, 3, 3)
        painter.setFont(self._font)
        text = QColor('#e6e6e6') if dark else QColor('#202020')
        if self.dim:
            text.setAlpha(100)
        painter.setPen(text)
        painter.drawText(self._rect, Qt.AlignCenter, self.label)

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged:
            for e in self.edges:
                e.update_path()
        return super().itemChange(change, value)

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        if event.button() == Qt.LeftButton:
            self._on_moved(self)


class EdgeShape(QGraphicsPathItem):
    def __init__(self, src: NodeShape, dst: NodeShape, qos_problem: bool):
        super().__init__()
        self.src, self.dst = src, dst
        self.qos_problem = qos_problem
        src.edges.append(self)
        dst.edges.append(self)
        self.setZValue(0)
        self.set_style(False, False)
        self.update_path()

    def set_style(self, highlight: bool, dim: bool) -> None:
        if self.qos_problem:
            c = QColor(theme.COLORS['error'])
        elif highlight:
            c = QColor('#e67e22')
        else:
            c = QColor('#9a9a9a')
        if dim:
            c.setAlpha(50)
        self.setPen(QPen(c, 2.2 if highlight or self.qos_problem else 1.1))
        self._color = c

    def update_path(self) -> None:
        a = self.src.pos() + QPointF(self.src._rect.right(), 0)
        b = self.dst.pos() + QPointF(self.dst._rect.left(), 0)
        if b.x() < a.x():
            a = self.src.pos() + QPointF(self.src._rect.left(), 0)
            b = self.dst.pos() + QPointF(self.dst._rect.right(), 0)
        path = QPainterPath(a)
        dx = max(40.0, abs(b.x() - a.x()) * 0.5)
        sign = 1 if b.x() >= a.x() else -1
        path.cubicTo(a + QPointF(dx * sign, 0), b - QPointF(dx * sign, 0), b)
        # arrow head
        angle = math.atan2(0, sign)
        p1 = b - QPointF(math.cos(angle - 0.4) * 9, math.sin(angle - 0.4) * 9)
        p2 = b - QPointF(math.cos(angle + 0.4) * 9, math.sin(angle + 0.4) * 9)
        path.addPolygon(QPolygonF([b, p1, p2, b]))
        self.setPath(path)


class _View(QGraphicsView):
    def wheelEvent(self, event) -> None:
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)


# --------------------------------------------------------------------------- page

class GraphPage(BasePage):
    page_id = 'graph'
    title = 'Graph'

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self._snap: Optional[GraphSnapshot] = None
        self._view_data: Optional[GraphView] = None
        self._layout_key = None
        self._positions: dict[str, tuple[float, float]] = {}
        self._manual: dict[str, tuple[float, float]] = self._load_manual()
        self._shapes: dict[str, NodeShape] = {}
        self._layout_running = False
        self._focus = ''
        self._build()
        self.on_event(GraphChanged, self._on_graph)
        self._rebuild_timer = QTimer(self)
        self._rebuild_timer.setSingleShot(True)
        self._rebuild_timer.timeout.connect(self._rebuild)

    def _build(self) -> None:
        self.scene = QGraphicsScene(self)
        self.view = _View(self.scene)
        self.view.setRenderHint(QPainter.Antialiasing)
        self.view.setDragMode(QGraphicsView.ScrollHandDrag)
        self.view.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.scene.selectionChanged.connect(self._selection_changed)
        self.hide_debug = QCheckBox('Hide debug topics')
        self.hide_debug.setToolTip(', '.join(sorted(DEBUG_TOPICS)))
        self.hide_debug.setChecked(True)
        self.hide_hidden = QCheckBox('Hide hidden nodes')
        self.hide_hidden.setChecked(True)
        self.hide_dead = QCheckBox('Hide dead ends')
        self.hide_dead.setToolTip('Hide topics without publisher or without subscriber')
        self.hide_dead.setChecked(True)
        self.hide_isolated = QCheckBox('Hide isolated nodes')
        self.hide_isolated.setToolTip('Hide nodes without connections after the filters')
        self.hide_isolated.setChecked(True)
        self.ns_filter = FilterEdit('Namespace…')
        self.ns_filter.setMaximumWidth(160)
        self.text_filter = FilterEdit('Name filter…')
        self.text_filter.setMaximumWidth(200)
        self.neighbors = QCheckBox('Only neighbours of selection')
        self.depth = QSpinBox()
        self.depth.setRange(1, 2)
        self.depth.setToolTip('Depth (node hops)')
        for w in (self.hide_debug, self.hide_hidden, self.hide_dead, self.hide_isolated,
                  self.neighbors):
            w.toggled.connect(self._schedule)
        for w in (self.ns_filter, self.text_filter):
            w.textChanged.connect(self._schedule)
        self.depth.valueChanged.connect(self._schedule)
        self.status = QLabel('')
        relayout = button('Re-layout', self._relayout, 'Compute the layout again and forget '
                          'manual positions')
        fit = button('Fit', self._fit)
        export = button('Export…', self._export)
        self.info = QPlainTextEdit()
        self.info.setReadOnly(True)
        self.info.setObjectName('terminal')
        self.info.setPlaceholderText('Select a node or a topic')
        top = QHBoxLayout()
        for w in (self.hide_debug, self.hide_hidden, self.hide_dead, self.hide_isolated,
                  self.ns_filter, self.text_filter, self.neighbors, self.depth):
            top.addWidget(w)
        top.addStretch(1)
        for w in (relayout, fit, export):
            top.addWidget(w)
        split = QSplitter()
        split.addWidget(self.view)
        split.addWidget(self.info)
        split.setSizes([1000, 320])
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(split, 1)
        lay.addWidget(self.status)

    # ----------------------------------------------------------------- lifecycle

    def on_enter(self) -> None:
        self._snap = self.rt.graph.snapshot()
        self._rebuild()

    def focus_item(self, kind: str, name: str) -> None:
        key = ('n:' if kind == 'node' else 't:') + name
        shape = self._shapes.get(key)
        if shape is None:
            self.text_filter.setText(name)
            self._rebuild()
            shape = self._shapes.get(key)
        if shape is not None:
            self.scene.clearSelection()
            shape.setSelected(True)
            self.view.centerOn(shape)

    def _on_graph(self, ev: GraphChanged) -> None:
        self._snap = ev.graph
        if self.active:
            self._schedule()

    def _schedule(self, *_a) -> None:
        self._rebuild_timer.start(150)

    # ----------------------------------------------------------------- build

    def _current_view(self) -> GraphView:
        hidden = self.rt.graph.is_hidden_node if self.hide_hidden.isChecked() else (
            lambda _n: _n.startswith('/robot_dashboard_'))
        focus = self._focus if self.neighbors.isChecked() else ''
        return build_view(self._snap or GraphSnapshot(), self.hide_debug.isChecked(),
                          self.hide_dead.isChecked(), hidden, self.ns_filter.text().strip(),
                          self.text_filter.text().strip(), focus, self.depth.value(),
                          int(self.rt.config.get('graph.max_elements', 600)),
                          self.hide_isolated.isChecked())

    def _rebuild(self) -> None:
        v = self._current_view()
        self._view_data = v
        key = v.key()
        if key == self._layout_key:
            self._update_status()
            return
        missing = [k for k in v.nodes if k not in self._positions]
        if missing or self._layout_key is None:
            self._run_layout(v, key)
        else:
            self._layout_key = key
            self._draw(v)

    def _run_layout(self, v: GraphView, key) -> None:
        if self._layout_running:
            self._schedule()
            return
        self._layout_running = True
        self.status.setText('computing layout…')

        def ok(pos):
            self._layout_running = False
            self._positions.update(pos)
            self._layout_key = key
            self._draw(v)
            if len(self.scene.items()) and not self._shapes_were_drawn:
                self._fit()

        def err(exc):
            self._layout_running = False
            self.status.setText(f'layout failed: {exc}')

        self._shapes_were_drawn = bool(self._shapes)
        self.bridge.run(layout_with_dot, v, on_ok=ok, on_err=err)

    def _draw(self, v: GraphView) -> None:
        selected = {s.key for s in self._shapes.values() if s.isSelected()}
        self.scene.blockSignals(True)
        self.scene.clear()
        self._shapes = {}
        for k, label in v.nodes.items():
            s = NodeShape(k, label, k.startswith('n:'), self._moved)
            x, y = self._manual.get(k, self._positions.get(k, (0.0, 0.0)))
            s.setPos(x, y)
            self.scene.addItem(s)
            self._shapes[k] = s
        for a, b, q in v.edges:
            self.scene.addItem(EdgeShape(self._shapes[a], self._shapes[b], q))
        for k in selected:
            if k in self._shapes:
                self._shapes[k].setSelected(True)
        self.scene.blockSignals(False)
        self._selection_changed()
        self._update_status()

    def _update_status(self) -> None:
        v = self._view_data
        if v is None:
            return
        n_nodes = sum(1 for k in v.nodes if k.startswith('n:'))
        txt = f'{n_nodes} nodes, {len(v.nodes) - n_nodes} topics, {len(v.edges)} connections'
        bad = sum(1 for e in v.edges if e[2])
        if bad:
            txt += f' — {bad} with QoS problems (red)'
        if v.truncated:
            txt += ' — TRUNCATED: too many elements, use the filters'
        if shutil.which('dot') is None:
            txt += ' — Graphviz "dot" not found: simple layout'
        self.status.setText(txt)

    def _relayout(self) -> None:
        self._manual.clear()
        self._save_manual()
        self._positions.clear()
        self._layout_key = None
        self._rebuild()

    def _fit(self) -> None:
        r = self.scene.itemsBoundingRect()
        if not r.isEmpty():
            self.view.fitInView(r.adjusted(-30, -30, 30, 30), Qt.KeepAspectRatio)

    # ----------------------------------------------------------------- interaction

    def _moved(self, shape: NodeShape) -> None:
        self._manual[shape.key] = (shape.pos().x(), shape.pos().y())
        self._save_manual()

    def _selection_changed(self) -> None:
        sel = [s for s in self._shapes.values() if s.isSelected()]
        connected: set[str] = set()
        for s in sel:
            for e in s.edges:
                connected.add(e.src.key)
                connected.add(e.dst.key)
        any_sel = bool(sel)
        for s in self._shapes.values():
            s.highlight = s.key in connected and not s.isSelected()
            s.dim = any_sel and s.key not in connected and not s.isSelected()
            s.update()
        for item in self.scene.items():
            if isinstance(item, EdgeShape):
                on = item.src.isSelected() or item.dst.isSelected()
                item.set_style(on, any_sel and not on)
        if sel:
            new_focus = sel[0].key
            if self.neighbors.isChecked() and new_focus != self._focus:
                self._focus = new_focus
                self._schedule()
            self._focus = new_focus
            self._show_info(sel[0])
        else:
            self.info.clear()

    def _show_info(self, shape: NodeShape) -> None:
        name = shape.key[2:]
        snap = self._snap or GraphSnapshot()
        if shape.is_node:
            itf = self.rt.graph.node_interfaces(name)
            lines = [f'node {name}', '']
            for title, key in (('publishes', 'publishers'), ('subscribes', 'subscribers'),
                               ('services', 'services'), ('action servers', 'action_servers'),
                               ('action clients', 'action_clients')):
                items = itf.get(key, [])
                lines.append(f'{title} ({len(items)}):')
                lines += [f'  {x}' for x in items]
            self.info.setPlainText('\n'.join(lines))
        else:
            t = snap.topic(name)
            if t is None:
                return
            lines = [f'topic {t.name}', f'type {t.type}', '', 'publishers:']
            lines += [f'  {p.node_fqn}  [{p.qos.short()}]' for p in t.publishers]
            lines.append('subscribers:')
            lines += [f'  {s.node_fqn}  [{s.qos.short()}]' for s in t.subscribers]
            issues = qos_issues(t)
            if issues:
                lines.append('')
                lines.append('QoS problems:')
                lines += [f'  {i.publisher} -> {i.subscriber}: {i.reason}' for i in issues]
            self.info.setPlainText('\n'.join(lines))

    # ----------------------------------------------------------------- persistence

    def _layout_file(self) -> str:
        return os.path.join(state_dir(), 'graph_layout.json')

    def _load_manual(self) -> dict:
        try:
            with open(self._layout_file(), encoding='utf-8') as f:
                return {k: tuple(v) for k, v in json.load(f).items()}
        except (OSError, ValueError):
            return {}

    def _save_manual(self) -> None:
        try:
            os.makedirs(state_dir(), exist_ok=True)
            with open(self._layout_file(), 'w', encoding='utf-8') as f:
                json.dump(self._manual, f)
        except OSError:
            pass

    # ----------------------------------------------------------------- export

    def _export(self) -> None:
        path, flt = QFileDialog.getSaveFileName(self, 'Export graph', 'ros_graph.png',
                                                'PNG image (*.png);;SVG (*.svg);;DOT (*.dot)')
        if not path:
            return
        try:
            self.export(path)
        except Exception as exc:  # noqa: BLE001
            self.error('Export', exc)
            return
        self.message(f'Graph exported to {path}')

    def export(self, path: str) -> None:
        rect = self.scene.itemsBoundingRect().adjusted(-20, -20, 20, 20)
        if path.endswith('.dot'):
            with open(path, 'w', encoding='utf-8') as f:
                f.write(export_dot(self._view_data or GraphView()))
        elif path.endswith('.svg'):
            from PySide6.QtSvg import QSvgGenerator
            gen = QSvgGenerator()
            gen.setFileName(path)
            gen.setSize(rect.size().toSize())
            gen.setViewBox(QRectF(0, 0, rect.width(), rect.height()))
            p = QPainter(gen)
            self.scene.render(p, QRectF(0, 0, rect.width(), rect.height()), rect)
            p.end()
        else:
            img = QImage(rect.size().toSize() * 2, QImage.Format_ARGB32)
            img.fill(Qt.white if theme.current() == 'light' else QColor(42, 42, 46))
            p = QPainter(img)
            p.setRenderHint(QPainter.Antialiasing)
            self.scene.render(p, QRectF(0, 0, img.width(), img.height()), rect)
            p.end()
            img.save(path)
