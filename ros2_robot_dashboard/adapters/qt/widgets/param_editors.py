"""Parameter editors and the delegate that picks one by type (design v1, section 5.9)."""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QModelIndex, Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QHBoxLayout, QLineEdit, QPushButton, QSlider,
                               QSpinBox, QStyledItemDelegate, QTableWidget, QTableWidgetItem,
                               QToolButton, QVBoxLayout, QWidget)

from ....core.domain.params import (ParamDescriptorInfo, ParamInfo, ParamType, coerce_text,
                                    format_value)
from ..models.param_model import ItemRole, ParamItem

INT32_MAX = 2 ** 31 - 1


class ParamEditor(QWidget):
    value_changed = Signal(object)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setAutoFillBackground(True)
        self._lay = QHBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(4)

    def set_value(self, value: Any) -> None:
        raise NotImplementedError

    def value(self) -> Any:
        raise NotImplementedError

    def set_descriptor(self, descriptor: ParamDescriptorInfo) -> None:
        pass


class BoolEditor(ParamEditor):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.box = QCheckBox()
        self._lay.addWidget(self.box)
        self._lay.addStretch(1)
        self.box.toggled.connect(lambda v: self.value_changed.emit(v))

    def set_value(self, value):
        self.box.blockSignals(True)
        self.box.setChecked(bool(value))
        self.box.blockSignals(False)

    def value(self):
        return self.box.isChecked()


class IntEditor(ParamEditor):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.spin = QSpinBox()
        self.spin.setRange(-INT32_MAX - 1, INT32_MAX)
        self.slider: Optional[QSlider] = None
        self._lay.addWidget(self.spin, 1)
        self.spin.valueChanged.connect(self._spin_changed)

    def set_descriptor(self, d):
        if d.int_range is not None:
            r = d.int_range
            lo, hi = max(r.from_value, -INT32_MAX - 1), min(r.to_value, INT32_MAX)
            self.spin.setRange(lo, hi)
            if r.step:
                self.spin.setSingleStep(r.step)
            self.slider = QSlider(Qt.Horizontal)
            self.slider.setRange(lo, hi)
            if r.step:
                self.slider.setSingleStep(r.step)
                self.slider.setPageStep(max(r.step, (hi - lo) // 10))
            self.slider.valueChanged.connect(self._slider_changed)
            self._lay.insertWidget(0, self.slider, 2)

    def _spin_changed(self, v):
        if self.slider is not None:
            self.slider.blockSignals(True)
            self.slider.setValue(v)
            self.slider.blockSignals(False)
        self.value_changed.emit(v)

    def _slider_changed(self, v):
        self.spin.setValue(v)

    def set_value(self, value):
        for w in (self.spin, self.slider):
            if w is not None:
                w.blockSignals(True)
                w.setValue(int(value or 0))
                w.blockSignals(False)

    def value(self):
        return self.spin.value()


class DoubleEditor(ParamEditor):
    STEPS = 1000

    def __init__(self, parent=None):
        super().__init__(parent)
        self.spin = QDoubleSpinBox()
        self.spin.setDecimals(6)
        self.spin.setRange(-1e300, 1e300)
        self.spin.setStepType(QDoubleSpinBox.AdaptiveDecimalStepType)
        self.slider: Optional[QSlider] = None
        self._lo, self._step = 0.0, 1.0
        self._lay.addWidget(self.spin, 1)
        self.spin.valueChanged.connect(self._spin_changed)

    def set_descriptor(self, d):
        if d.float_range is not None:
            r = d.float_range
            self.spin.setRange(r.from_value, r.to_value)
            self._lo = r.from_value
            span = r.to_value - r.from_value
            # Slider works in integers: use the descriptor step, or 1000 steps.
            self._step = r.step if r.step else (span / self.STEPS if span > 0 else 1.0)
            if r.step:
                self.spin.setSingleStep(r.step)
            n = int(round(span / self._step)) if self._step > 0 else self.STEPS
            self.slider = QSlider(Qt.Horizontal)
            self.slider.setRange(0, max(1, n))
            self.slider.valueChanged.connect(self._slider_changed)
            self._lay.insertWidget(0, self.slider, 2)

    def _spin_changed(self, v):
        if self.slider is not None:
            self.slider.blockSignals(True)
            self.slider.setValue(int(round((v - self._lo) / self._step)))
            self.slider.blockSignals(False)
        self.value_changed.emit(v)

    def _slider_changed(self, i):
        self.spin.setValue(self._lo + i * self._step)

    def set_value(self, value):
        v = float(value or 0.0)
        self.spin.blockSignals(True)
        self.spin.setValue(v)
        self.spin.blockSignals(False)
        if self.slider is not None:
            self.slider.blockSignals(True)
            self.slider.setValue(int(round((v - self._lo) / self._step)))
            self.slider.blockSignals(False)

    def value(self):
        return self.spin.value()


class StringEditor(ParamEditor):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.edit = QLineEdit()
        self._lay.addWidget(self.edit)
        self.edit.editingFinished.connect(lambda: self.value_changed.emit(self.edit.text()))

    def set_value(self, value):
        self.edit.setText('' if value is None else str(value))

    def value(self):
        return self.edit.text()


class ArrayEditor(ParamEditor):
    """Text field with parsing (``1, 2, 3``) plus a table dialog."""

    def __init__(self, ptype: ParamType, parent=None):
        super().__init__(parent)
        self.ptype = ptype
        self.edit = QLineEdit()
        btn = QToolButton()
        btn.setText('…')
        btn.setToolTip('Edit as a table')
        btn.clicked.connect(self._open_table)
        self._lay.addWidget(self.edit, 1)
        self._lay.addWidget(btn)
        self.edit.editingFinished.connect(lambda: self.value_changed.emit(self.edit.text()))

    def set_value(self, value):
        if isinstance(value, str):
            self.edit.setText(value)
        else:
            self.edit.setText(format_value(value or [], self.ptype))

    def value(self):
        return self.edit.text()

    def _open_table(self):
        try:
            values = coerce_text(self.edit.text(), self.ptype)
        except ValueError:
            values = []
        dlg = ArrayDialog(self.ptype, values, self)
        if dlg.exec() == QDialog.Accepted:
            self.edit.setText(format_value(dlg.values(), self.ptype))
            self.value_changed.emit(self.edit.text())


class ArrayDialog(QDialog):
    def __init__(self, ptype: ParamType, values: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Edit array')
        self.ptype = ptype
        self.table = QTableWidget(len(values), 1)
        self.table.setHorizontalHeaderLabels(['value'])
        self.table.horizontalHeader().setStretchLastSection(True)
        for i, v in enumerate(values):
            self.table.setItem(i, 0, QTableWidgetItem(format_value(v, _elem(ptype))))
        add = QPushButton('Add')
        add.clicked.connect(lambda: self.table.insertRow(self.table.rowCount()))
        rem = QPushButton('Remove')
        rem.clicked.connect(lambda: [self.table.removeRow(r.row()) for r in sorted(
            self.table.selectionModel().selectedRows(), key=lambda x: -x.row())])
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(self.table)
        row = QHBoxLayout()
        row.addWidget(add)
        row.addWidget(rem)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(bb)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.resize(320, 400)

    def values(self) -> list:
        texts = []
        for i in range(self.table.rowCount()):
            it = self.table.item(i, 0)
            texts.append(it.text() if it else '')
        quoted = ', '.join(f'"{t}"' if self.ptype == ParamType.STRING_ARRAY else t for t in texts)
        return coerce_text('[' + quoted + ']', self.ptype)


def _elem(ptype: ParamType) -> ParamType:
    return {ParamType.BOOL_ARRAY: ParamType.BOOL, ParamType.INTEGER_ARRAY: ParamType.INTEGER,
            ParamType.DOUBLE_ARRAY: ParamType.DOUBLE, ParamType.STRING_ARRAY: ParamType.STRING,
            ParamType.BYTE_ARRAY: ParamType.INTEGER}.get(ptype, ParamType.STRING)


def create_editor(info: ParamInfo, parent=None) -> ParamEditor:
    t = info.type
    if t == ParamType.BOOL:
        ed = BoolEditor(parent)
    elif t == ParamType.INTEGER:
        ed = IntEditor(parent)
    elif t == ParamType.DOUBLE:
        ed = DoubleEditor(parent)
    elif t == ParamType.STRING:
        ed = StringEditor(parent)
    else:
        ed = ArrayEditor(t if t != ParamType.NOT_SET else ParamType.STRING_ARRAY, parent)
    ed.set_descriptor(info.descriptor)
    return ed


class ParamDelegate(QStyledItemDelegate):
    """Creates the editor for the value column. ``setModelData`` only marks MODIFIED."""

    edited = Signal(QModelIndex, object)      # (source index, value)

    def __init__(self, proxy=None, parent=None):
        super().__init__(parent)
        self._proxy = proxy

    def _source(self, index: QModelIndex) -> QModelIndex:
        if self._proxy is not None and index.model() is self._proxy:
            return self._proxy.mapToSource(index)
        return index

    def createEditor(self, parent, option, index):
        item = index.data(ItemRole)
        if not isinstance(item, ParamItem):
            return None
        ed = create_editor(item.info, parent)
        if item.info.type in (ParamType.BOOL, ParamType.INTEGER, ParamType.DOUBLE):
            # Live updates for sliders/spins: the controller debounces them.
            ed.value_changed.connect(lambda v, i=index, e=ed: self._live(e, i))
        return ed

    def _live(self, editor, index):
        self.commitData.emit(editor)

    def setEditorData(self, editor, index):
        item = index.data(ItemRole)
        if isinstance(item, ParamItem) and isinstance(editor, ParamEditor):
            editor.set_value(item.display_value)

    def setModelData(self, editor, model, index):
        if not isinstance(editor, ParamEditor):
            return
        value = editor.value()
        self.edited.emit(self._source(index), value)

    def updateEditorGeometry(self, editor, option, index):
        editor.setGeometry(option.rect)
