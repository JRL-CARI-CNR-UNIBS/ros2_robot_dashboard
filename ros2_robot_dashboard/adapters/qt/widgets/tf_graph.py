"""Graphical TF tree (like ``view_frames``): frames as boxes, parent -> child arrows."""

from __future__ import annotations

import time
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetricsF, QImage, QPainter, QPainterPath,
                           QPen, QPolygonF)
from PySide6.QtWidgets import (QGraphicsItem, QGraphicsPathItem, QGraphicsScene,
                               QGraphicsSimpleTextItem, QGraphicsView)

from ....core.domain.tf import TfTreeSnapshot
from .. import theme

H_GAP = 24.0        # between sibling subtrees
V_GAP = 70.0        # between levels


def frame_label_width(name: str, font: QFont) -> float:
    return QFontMetricsF(font).horizontalAdvance(name) + 24.0


def tree_layout(snap: TfTreeSnapshot, widths: dict[str, float]) -> dict[str, tuple[float, float]]:
    """Top-down tree layout: each parent centred above its children.

    Frames referenced only as a parent (never received) are roots too. Returns
    ``{frame: (x_centre, depth)}``; loops are cut.
    """
    children: dict[str, list[str]] = {}
    known = {f.name for f in snap.frames}
    for f in snap.frames:
        if f.parent:
            children.setdefault(f.parent, []).append(f.name)
    for v in children.values():
        v.sort()
    roots = sorted({f.name for f in snap.frames if not f.parent} |
                   {f.parent for f in snap.frames if f.parent and f.parent not in known})
    # Frames only reachable through a loop get no root: add them so they are drawn.
    reach: set[str] = set()

    def mark(n: str) -> None:
        if n in reach:
            return
        reach.add(n)
        for c in children.get(n, []):
            mark(c)

    for r in roots:
        mark(r)
    roots += sorted(known - reach)

    pos: dict[str, tuple[float, float]] = {}
    sub_w: dict[str, float] = {}
    visiting: set[str] = set()

    def width(n: str) -> float:
        if n in sub_w:
            return sub_w[n]
        visiting.add(n)
        kids = [c for c in children.get(n, []) if c not in visiting]
        w_kids = sum(width(c) for c in kids) + H_GAP * max(0, len(kids) - 1)
        visiting.discard(n)
        sub_w[n] = max(widths.get(n, 80.0), w_kids)
        return sub_w[n]

    def place(n: str, left: float, depth: int) -> None:
        if n in pos:
            return
        w = width(n)
        pos[n] = (left + w / 2.0, float(depth))
        kids = [c for c in children.get(n, []) if c not in pos]
        total = sum(width(c) for c in kids) + H_GAP * max(0, len(kids) - 1)
        x = left + (w - total) / 2.0
        for c in kids:
            place(c, x, depth + 1)
            x += width(c) + H_GAP

    left = 0.0
    for r in roots:
        if r in pos:
            continue
        place(r, left, 0)
        left += width(r) + H_GAP * 2
    return pos


def tf_to_dot(snap: TfTreeSnapshot) -> str:
    lines = ['digraph tf {', '  node [shape=box, style=rounded];']
    for f in sorted(snap.frames, key=lambda f: f.name):
        if f.parent:
            label = 'static' if f.is_static else f'{f.rate_hz:.1f} Hz'
            lines.append(f'  "{f.parent}" -> "{f.name}" [label="{label}"];')
        else:
            lines.append(f'  "{f.name}";')
    lines.append('}')
    return '\n'.join(lines) + '\n'


class FrameBox(QGraphicsItem):
    def __init__(self, name: str, font: QFont, width: float):
        super().__init__()
        self.name = name
        self._font = font
        h = QFontMetricsF(font).height() + 12
        self._rect = QRectF(-width / 2, -h / 2, width, h)
        self.problem = False
        self.missing = False         # referenced as parent, never received
        self.static = False
        self.setFlags(QGraphicsItem.ItemIsSelectable)
        self.setCursor(Qt.PointingHandCursor)
        self.setZValue(1)

    def boundingRect(self) -> QRectF:
        return self._rect.adjusted(-2, -2, 2, 2)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        dark = theme.current() == 'dark'
        fill = QColor('#2d4a73') if dark else QColor('#dce9fb')
        border = QColor('#6ea0e6') if dark else QColor('#3a7bd5')
        if self.static:
            fill = QColor('#3a3a40') if dark else QColor('#eeeeee')
            border = QColor('#9a9a9a')
        pen = QPen(border, 1.3)
        if self.missing:
            pen.setStyle(Qt.DashLine)
        if self.problem:
            pen = QPen(QColor(theme.COLORS['error']), 2.2)
        if self.isSelected():
            pen = QPen(QColor('#e67e22'), 2.6)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(pen)
        painter.setBrush(QBrush(fill))
        painter.drawRoundedRect(self._rect, 6, 6)
        painter.setFont(self._font)
        painter.setPen(QColor('#e6e6e6') if dark else QColor('#202020'))
        painter.drawText(self._rect, Qt.AlignCenter, self.name)


class TfGraphView(QGraphicsView):
    """Shows a :class:`TfTreeSnapshot`; emits ``frame_clicked`` and ``frame_menu``."""

    frame_clicked = Signal(str)
    frame_double_clicked = Signal(str)
    frame_menu = Signal(str, object)          # frame, global position

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.Antialiasing)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self._font = QFont()
        self._font.setPointSize(9)
        self._small = QFont()
        self._small.setPointSize(8)
        self.boxes: dict[str, FrameBox] = {}
        self._labels: dict[str, QGraphicsSimpleTextItem] = {}
        self._structure: tuple = ()
        self._fitted = False
        self.snapshot = TfTreeSnapshot()

    # ----------------------------------------------------------------- data

    def set_snapshot(self, snap: TfTreeSnapshot, highlight: str = '') -> None:
        self.snapshot = snap
        known = {f.name for f in snap.frames}
        structure = tuple(sorted((f.name, f.parent) for f in snap.frames))
        if structure != self._structure:
            self._structure = structure
            self._rebuild(snap)
        problem = {fr for i in snap.issues for fr in i.frames}
        now = time.time()
        by_name = {f.name: f for f in snap.frames}
        for name, box in self.boxes.items():
            f = by_name.get(name)
            box.problem = name in problem
            box.missing = name not in known
            box.static = bool(f and f.is_static)
            if f is None:
                box.setToolTip(f'{name}\nreferenced as parent, never received')
            else:
                age = '' if f.is_static else f'\nlast update {now - f.last_update:.2f} s ago'
                extra = ''
                if len(f.parents_seen) > 1:
                    extra = '\nparents seen: ' + ', '.join(f.parents_seen)
                box.setToolTip(f'{name}\nparent: {f.parent or "(root)"}\n'
                               f'{"static" if f.is_static else f"{f.rate_hz:.1f} Hz"}'
                               f'{age}{extra}')
            box.update()
        for name, label in self._labels.items():
            f = by_name.get(name)
            if f is not None:
                label.setText('static' if f.is_static else f'{f.rate_hz:.1f} Hz')
        if highlight and highlight in self.boxes:
            self.select(highlight)
        if not self._fitted and self.boxes:
            self.fit()
            self._fitted = True

    def _rebuild(self, snap: TfTreeSnapshot) -> None:
        selected = [b.name for b in self.boxes.values() if b.isSelected()]
        scene = self.scene()
        scene.clear()
        self.boxes = {}
        self._labels = {}
        names = {f.name for f in snap.frames} | {f.parent for f in snap.frames if f.parent}
        widths = {n: frame_label_width(n, self._font) for n in names}
        pos = tree_layout(snap, widths)
        for n in names:
            x, depth = pos.get(n, (0.0, 0.0))
            box = FrameBox(n, self._font, widths[n])
            box.setPos(x, depth * V_GAP)
            scene.addItem(box)
            self.boxes[n] = box
        edge_color = QColor('#9a9a9a')
        for f in snap.frames:
            if not f.parent or f.parent not in self.boxes:
                continue
            a, b = self.boxes[f.parent], self.boxes[f.name]
            p1 = a.pos() + QPointF(0, a._rect.bottom())
            p2 = b.pos() + QPointF(0, b._rect.top())
            path = QPainterPath(p1)
            dy = (p2.y() - p1.y()) * 0.5
            path.cubicTo(p1 + QPointF(0, dy), p2 - QPointF(0, dy), p2 - QPointF(0, 7))
            item = QGraphicsPathItem(path)
            pen = QPen(edge_color, 1.1)
            if f.is_static:
                pen.setStyle(Qt.DashLine)
            item.setPen(pen)
            item.setBrush(Qt.NoBrush)
            item.setZValue(0)
            scene.addItem(item)
            head = QPainterPath()
            head.addPolygon(QPolygonF([p2, p2 + QPointF(-4, -8), p2 + QPointF(4, -8), p2]))
            arrow = QGraphicsPathItem(head)
            arrow.setPen(QPen(edge_color, 1.0))
            arrow.setBrush(QBrush(edge_color))
            scene.addItem(arrow)
            label = QGraphicsSimpleTextItem('')
            label.setFont(self._small)
            label.setBrush(QBrush(QColor('#8a8a8a')))
            mid = path.pointAtPercent(0.5)
            label.setPos(mid + QPointF(5, -7))
            label.setZValue(2)
            scene.addItem(label)
            self._labels[f.name] = label
        for n in selected:
            if n in self.boxes:
                self.boxes[n].setSelected(True)
        scene.setSceneRect(scene.itemsBoundingRect().adjusted(-40, -40, 40, 40))

    # ----------------------------------------------------------------- interaction

    def select(self, name: str) -> None:
        self.scene().clearSelection()
        box = self.boxes.get(name)
        if box is not None:
            box.setSelected(True)
            self.ensureVisible(box)

    def fit(self) -> None:
        rect = self.scene().itemsBoundingRect().adjusted(-20, -20, 20, 20)
        if not rect.isEmpty():
            self.fitInView(rect, Qt.KeepAspectRatio)
            # Do not blow small trees up beyond 1:1.
            if self.transform().m11() > 1.0:
                self.resetTransform()
                self.centerOn(rect.center())

    def _box_at(self, pos) -> Optional[FrameBox]:
        for item in self.items(pos):
            if isinstance(item, FrameBox):
                return item
        return None

    def mousePressEvent(self, event) -> None:
        box = self._box_at(event.position().toPoint())
        if box is not None and event.button() == Qt.LeftButton:
            self.select(box.name)
            self.frame_clicked.emit(box.name)
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        box = self._box_at(event.position().toPoint())
        if box is not None:
            self.frame_double_clicked.emit(box.name)
            return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event) -> None:
        box = self._box_at(event.pos())
        if box is not None:
            self.select(box.name)
            self.frame_menu.emit(box.name, event.globalPos())

    def wheelEvent(self, event) -> None:
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)

    # ----------------------------------------------------------------- export

    def export(self, path: str) -> None:
        scene = self.scene()
        rect = scene.itemsBoundingRect().adjusted(-20, -20, 20, 20)
        if path.endswith('.dot'):
            with open(path, 'w', encoding='utf-8') as f:
                f.write(tf_to_dot(self.snapshot))
        elif path.endswith('.svg'):
            from PySide6.QtSvg import QSvgGenerator
            gen = QSvgGenerator()
            gen.setFileName(path)
            gen.setSize(rect.size().toSize())
            gen.setViewBox(QRectF(0, 0, rect.width(), rect.height()))
            p = QPainter(gen)
            scene.render(p, QRectF(0, 0, rect.width(), rect.height()), rect)
            p.end()
        else:
            img = QImage(rect.size().toSize() * 2, QImage.Format_ARGB32)
            img.fill(Qt.white if theme.current() == 'light' else QColor(42, 42, 46))
            p = QPainter(img)
            p.setRenderHint(QPainter.Antialiasing)
            scene.render(p, QRectF(0, 0, img.width(), img.height()), rect)
            p.end()
            if not img.save(path):
                raise OSError(f'cannot write {path}')
