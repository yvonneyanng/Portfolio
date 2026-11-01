import maya.cmds as cmds
import maya.OpenMayaUI as omui

try:
    from PySide6 import QtWidgets, QtCore
    from shiboken6 import wrapInstance
except ImportError:
    from PySide2 import QtWidgets, QtCore
    from shiboken2 import wrapInstance

def get_maya_main_window() -> QtWidgets.QWidget:
    main_window_ptr = omui.MQtUtil.mainWindow()
    return wrapInstance(int(main_window_ptr), QtWidgets.QWidget)

class AnimLayersVariantSetter(QtWidgets.QDialog):

    window = None

    @classmethod
    def show_dialog(cls) -> None:
        if cls.window is not None:
            cls.window.close()
        cls.window = cls(parent=get_maya_main_window())
        cls.window.show()

    def __init__(self, parent: QtWidgets.QWidget = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Anim Layers Variant Setter")
        self.setMinimumWidth(250)
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.Tool)

        self.checkboxes: list[tuple[str, QtWidgets.QCheckBox]] = []
        self.build_ui()

    def is_base_layer(self, layer: str) -> bool:
        return cmds.animLayer(layer, query=True, parent=True) is None

    def build_ui(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)

        header_row = QtWidgets.QHBoxLayout()
        header = QtWidgets.QLabel('Select layers to mark as variants:')
        header.setStyleSheet('font-size: 14px; font-weight: bold;')
        header_row.addWidget(header)
        header_row.addStretch()
        refresh_btn = QtWidgets.QPushButton('Refresh Layers')
        refresh_btn.setFixedWidth(90)
        refresh_btn.clicked.connect(self.refresh_all)
        header_row.addWidget(refresh_btn)
        layout.addLayout(header_row)

        self.layers_layout = QtWidgets.QVBoxLayout()
        self.refresh_layers()
        layout.addLayout(self.layers_layout)

        apply_btn = QtWidgets.QPushButton('Apply Variants')
        apply_btn.clicked.connect(self.mark_variants)
        layout.addWidget(apply_btn)

        divider = QtWidgets.QFrame()
        divider.setFrameShape(QtWidgets.QFrame.HLine)
        divider.setFrameShadow(QtWidgets.QFrame.Sunken)
        layout.addWidget(divider)

        variants_header = QtWidgets.QLabel('Current variants list:')
        variants_header.setStyleSheet('font-size: 14px; font-weight: bold;')
        layout.addWidget(variants_header)

        self.variants_list_layout = QtWidgets.QVBoxLayout()
        self.refresh_variants_list()
        layout.addLayout(self.variants_list_layout)

    def is_variant(self, layer: str) -> bool:
        try:
            if cmds.attributeQuery('isVariant', node=layer, exists=True):
                return cmds.getAttr(layer + '.isVariant')
        except Exception:
            pass
        return False

    def refresh_all(self) -> None:
        self.refresh_layers()
        self.refresh_variants_list()
        QtCore.QTimer.singleShot(0, self.adjustSize)

    def _clear_layout(self, layout: QtWidgets.QLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item.layout():
                while item.layout().count():
                    w = item.layout().takeAt(0).widget()
                    if w:
                        w.deleteLater()
            elif item.widget():
                item.widget().deleteLater()

    def refresh_layers(self) -> None:
        self._clear_layout(self.layers_layout)
        self.checkboxes = []
        layers: list[str] = [layer for layer in (cmds.ls(type='animLayer') or []) if not self.is_base_layer(layer)]

        if not layers:
            self.layers_layout.addWidget(QtWidgets.QLabel('No anim layers found.'))
            QtCore.QTimer.singleShot(0, self.adjustSize)
            return

        for layer in layers:
            try:
                is_var: bool = self.is_variant(layer)
                row = QtWidgets.QHBoxLayout()
                cb = QtWidgets.QCheckBox(layer)
                cb.setChecked(is_var)
                row.addWidget(cb)
                row.addStretch()
                self.checkboxes.append((layer, cb))
                self.layers_layout.addLayout(row)
            except Exception as e:
                cmds.warning("[{0}] Skipping layer: {1}".format(layer, e))

        QtCore.QTimer.singleShot(0, self.adjustSize)

    def refresh_variants_list(self) -> None:
        self._clear_layout(self.variants_list_layout)
        layers: list[str] = [layer for layer in (cmds.ls(type='animLayer') or []) if not self.is_base_layer(layer)]
        variants: list[str] = [layer for layer in layers if self.is_variant(layer)]

        if not variants:
            self.variants_list_layout.addWidget(QtWidgets.QLabel('No layers marked as variants yet.'))
            QtCore.QTimer.singleShot(0, self.adjustSize)
            return

        for v in variants:
            row = QtWidgets.QHBoxLayout()
            label = QtWidgets.QLabel(v)
            row.addWidget(label)
            row.addStretch()
            self.variants_list_layout.addLayout(row)

        QtCore.QTimer.singleShot(0, self.adjustSize)

    def mark_variants(self) -> None:
        for layer, cb in self.checkboxes:
            try:
                if not cmds.objExists(layer):
                    cmds.warning("[{0}] This layer no longer exists.".format(layer))
                    continue

                if cb.isChecked():
                    if not cmds.attributeQuery('isVariant', node=layer, exists=True):
                        cmds.addAttr(layer, longName='isVariant', attributeType='bool')
                    cmds.setAttr(layer + '.isVariant', True)
                else:
                    if cmds.attributeQuery('isVariant', node=layer, exists=True):
                        cmds.deleteAttr(layer + '.isVariant')

            except Exception as e:
                cmds.warning("[{0}] Error marking layer: {1}".format(layer, e))

        self.refresh_layers()
        self.refresh_variants_list()
        QtCore.QTimer.singleShot(0, self.adjustSize)