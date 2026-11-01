import math
import maya.cmds as cmds
import maya.api.OpenMaya as om
import maya.OpenMayaUI as omui

try:
    from PySide6 import QtWidgets, QtCore
    from shiboken6 import wrapInstance
except ImportError:
    from PySide2 import QtWidgets, QtCore
    from shiboken2 import wrapInstance

EPSILON = 1e-8

def get_maya_main_window():
    main_window_ptr = omui.MQtUtil.mainWindow()
    return wrapInstance(int(main_window_ptr), QtWidgets.QWidget)

def make_plane(plane_axis, name, width, start_pos, end_pos):

    # THE MATH:
    # 1. Calculate the three axes of the plane and its midpoint
    # 2. Build a rotation matrix from the three axes of the plane
    # 3. Convert the matrix rotation to euler angles in degrees
    
    chain_dir = (end_pos - start_pos).normalize()
    length = (end_pos - start_pos).length()
    midpoint = (start_pos + end_pos) / 2.0

    mat = om.MMatrix([
        plane_axis.x, plane_axis.y, plane_axis.z, 0,
        (chain_dir ^ plane_axis).x, (chain_dir ^ plane_axis).y, (chain_dir ^ plane_axis).z, 0,
        chain_dir.x, chain_dir.y, chain_dir.z, 0,
        0, 0, 0, 1
    ])

    transform = om.MTransformationMatrix(mat)
    euler = transform.rotation()
    rx = math.degrees(euler.x)
    ry = math.degrees(euler.y)
    rz = math.degrees(euler.z)

    plane, _ = cmds.polyPlane(
        name=name,
        width=width * 2,
        height=length,
        subdivisionsX=5,
        subdivisionsY=5,
        axis=[0, 1, 0]
    )

    cmds.setAttr(f"{plane}.rotateX", rx)
    cmds.setAttr(f"{plane}.rotateY", ry)
    cmds.setAttr(f"{plane}.rotateZ", rz)
    cmds.setAttr(f"{plane}.translateX", midpoint.x)
    cmds.setAttr(f"{plane}.translateY", midpoint.y)
    cmds.setAttr(f"{plane}.translateZ", midpoint.z)

    shape = cmds.listRelatives(plane, shapes=True)[0]
    cmds.setAttr(f"{shape}.displayEdges", 1)

    return plane


class JointAlignment(QtWidgets.QDialog):
    
    window = None

    @classmethod
    def show_dialog(cls):
        if cls.window is not None:
            cls.window.close()
        cls.window = cls(parent=get_maya_main_window())
        cls.window.show()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Joints Alignment")
        self.setMinimumWidth(320)
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.Tool)

        self.build_ui()
        self._restore_groups()
        self._node_removed_cb = om.MDGMessage.addNodeRemovedCallback(self._on_node_removed, "transform")

    def build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(6)

        layout.addWidget(QtWidgets.QLabel("Alignment View"))

        self.view_btn_group = QtWidgets.QButtonGroup()
        view_row = QtWidgets.QHBoxLayout()
        for i, label in enumerate(["Front / Back", "Left / Right", "Top / Bottom"]):
            btn = QtWidgets.QRadioButton(label)
            self.view_btn_group.addButton(btn, i)
            view_row.addWidget(btn)
        layout.addLayout(view_row)
        self.view_btn_group.button(0).setChecked(True)

        line0 = QtWidgets.QFrame()
        line0.setFrameShape(QtWidgets.QFrame.HLine)
        line0.setFrameShadow(QtWidgets.QFrame.Sunken)
        layout.addWidget(line0)

        dropdown_row = QtWidgets.QHBoxLayout()
        self.dropdown = QtWidgets.QComboBox()
        self.dropdown.setFixedHeight(28)
        self.dropdown.currentIndexChanged.connect(self.on_group_changed)
        dropdown_row.addWidget(self.dropdown)
        layout.addLayout(dropdown_row)

        btn_row = QtWidgets.QHBoxLayout()
        self.create_btn = QtWidgets.QPushButton("Create Planes")
        self.create_btn.setFixedHeight(30)
        self.create_btn.clicked.connect(self.create_group)
        btn_row.addWidget(self.create_btn)

        self.align_btn = QtWidgets.QPushButton("Align Joints")
        self.align_btn.setFixedHeight(30)
        self.align_btn.clicked.connect(self.align_joints)
        btn_row.addWidget(self.align_btn)

        self.delete_btn = QtWidgets.QPushButton("Delete Group")
        self.delete_btn.setFixedHeight(30)
        self.delete_btn.setEnabled(False)
        self.delete_btn.clicked.connect(self.delete_group)
        btn_row.addWidget(self.delete_btn)
        layout.addLayout(btn_row)

        line1 = QtWidgets.QFrame()
        line1.setFrameShape(QtWidgets.QFrame.HLine)
        line1.setFrameShadow(QtWidgets.QFrame.Sunken)
        layout.addWidget(line1)

        layout.addWidget(QtWidgets.QLabel("Alignment Plane"))

        row = QtWidgets.QHBoxLayout()
        self.toggle_align_btn = QtWidgets.QPushButton("Hide")
        self.toggle_align_btn.setFixedHeight(28)
        self.toggle_align_btn.setCheckable(True)
        self.toggle_align_btn.setEnabled(False)
        self.toggle_align_btn.clicked.connect(lambda checked: self.toggle_plane("align", checked))
        row.addWidget(self.toggle_align_btn)

        self.flip_align_btn = QtWidgets.QPushButton("Flip")
        self.flip_align_btn.setFixedHeight(28)
        self.flip_align_btn.setEnabled(False)
        self.flip_align_btn.clicked.connect(lambda: self.flip_normal("align"))
        row.addWidget(self.flip_align_btn)
        layout.addLayout(row)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Subdivision Width"))
        self.sub_align_width = QtWidgets.QSpinBox()
        self.sub_align_width.setRange(1, 50)
        self.sub_align_width.setValue(3)
        self.sub_align_width.setEnabled(False)
        self.sub_align_width.valueChanged.connect(lambda: self.update_subdivisions("align"))
        row.addWidget(self.sub_align_width)
        layout.addLayout(row)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Subdivision Height"))
        self.sub_align_height = QtWidgets.QSpinBox()
        self.sub_align_height.setRange(1, 50)
        self.sub_align_height.setValue(3)
        self.sub_align_height.setEnabled(False)
        self.sub_align_height.valueChanged.connect(lambda: self.update_subdivisions("align"))
        row.addWidget(self.sub_align_height)
        layout.addLayout(row)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Plane Width"))
        self.width_align = QtWidgets.QDoubleSpinBox()
        self.width_align.setRange(0.1, 10000.0)
        self.width_align.setDecimals(2)
        self.width_align.setSingleStep(1.0)
        self.width_align.setValue(15.0)
        self.width_align.setEnabled(False)
        self.width_align.valueChanged.connect(lambda: self.update_plane_width("align"))
        row.addWidget(self.width_align)
        layout.addLayout(row)

        line2 = QtWidgets.QFrame()
        line2.setFrameShape(QtWidgets.QFrame.HLine)
        line2.setFrameShadow(QtWidgets.QFrame.Sunken)
        layout.addWidget(line2)

        layout.addWidget(QtWidgets.QLabel("Perpendicular Plane"))

        row = QtWidgets.QHBoxLayout()
        self.toggle_perp_btn = QtWidgets.QPushButton("Hide")
        self.toggle_perp_btn.setFixedHeight(28)
        self.toggle_perp_btn.setCheckable(True)
        self.toggle_perp_btn.setEnabled(False)
        self.toggle_perp_btn.clicked.connect(lambda checked: self.toggle_plane("perp", checked))
        row.addWidget(self.toggle_perp_btn)

        self.flip_perp_btn = QtWidgets.QPushButton("Flip")
        self.flip_perp_btn.setFixedHeight(28)
        self.flip_perp_btn.setEnabled(False)
        self.flip_perp_btn.clicked.connect(lambda: self.flip_normal("perp"))
        row.addWidget(self.flip_perp_btn)
        layout.addLayout(row)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Subdivision Width"))
        self.sub_perp_width = QtWidgets.QSpinBox()
        self.sub_perp_width.setRange(1, 50)
        self.sub_perp_width.setValue(3)
        self.sub_perp_width.setEnabled(False)
        self.sub_perp_width.valueChanged.connect(lambda: self.update_subdivisions("perp"))
        row.addWidget(self.sub_perp_width)
        layout.addLayout(row)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Subdivision Height"))
        self.sub_perp_height = QtWidgets.QSpinBox()
        self.sub_perp_height.setRange(1, 50)
        self.sub_perp_height.setValue(3)
        self.sub_perp_height.setEnabled(False)
        self.sub_perp_height.valueChanged.connect(lambda: self.update_subdivisions("perp"))
        row.addWidget(self.sub_perp_height)
        layout.addLayout(row)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Plane Width"))
        self.width_perp = QtWidgets.QDoubleSpinBox()
        self.width_perp.setRange(0.1, 10000.0)
        self.width_perp.setDecimals(2)
        self.width_perp.setSingleStep(1.0)
        self.width_perp.setValue(15.0)
        self.width_perp.setEnabled(False)
        self.width_perp.valueChanged.connect(lambda: self.update_plane_width("perp"))
        row.addWidget(self.width_perp)
        layout.addLayout(row)

    def _get_vectors(self):
        selected = cmds.ls(selection=True, type='joint', long=True)
        if len(selected) != 2:
            cmds.warning("Please select exactly two joints.")
            return None, None, None, None, None

        start_pos_raw = cmds.xform(selected[0], query=True, worldSpace=True, translation=True)
        end_pos_raw = cmds.xform(selected[1], query=True, worldSpace=True, translation=True)
        joint_short_names = [selected[0].split("|")[-1], selected[1].split("|")[-1]]

        start_pos = om.MVector(*start_pos_raw)
        end_pos = om.MVector(*end_pos_raw)

        view = self.view_btn_group.checkedId()
        if view == 0:
            view_normal = om.MVector(0, 0, 1)
        elif view == 1:
            view_normal = om.MVector(1, 0, 0)
        else:
            view_normal = om.MVector(0, 1, 0)

        chain_vec = end_pos - start_pos
        length = chain_vec.length()
        if length < EPSILON:
            cmds.warning("Selected joints are at the same position.")
            return None, None, None, None, None
        chain_dir = chain_vec / length
        plane_perp_axis = chain_dir ^ view_normal
        if plane_perp_axis.length() < EPSILON:
            cmds.warning("Selected view is parallel to the joint chain. Choose a different view.")
            return None, None, None, None, None
        plane_perp_axis.normalize()
        plane_align_axis = chain_dir ^ plane_perp_axis
        plane_align_axis.normalize()

        return start_pos, end_pos, plane_align_axis, plane_perp_axis, joint_short_names

    def create_group(self):
        start_pos, end_pos, plane_align_axis, plane_perp_axis, joint_short_names = self._get_vectors()
        if start_pos is None:
            return

        group_name = f"{joint_short_names[0]}_{joint_short_names[1]}_planes"
        group = cmds.group(empty=True, name=group_name)
        group_name = group

        plane_width = 15.0
        plane_align = make_plane(plane_align_axis, f"{group_name}_align", plane_width, start_pos, end_pos)
        plane_perp = make_plane(plane_perp_axis, f"{group_name}_perp", plane_width, start_pos, end_pos)

        cmds.parent(plane_align, group)
        cmds.parent(plane_perp, group)

        self.dropdown.blockSignals(True)
        self.dropdown.addItem(group_name)
        self.dropdown.setCurrentText(group_name)
        self.dropdown.blockSignals(False)
        self.on_group_changed()
        self.delete_btn.setEnabled(True)

    def _restore_groups(self):
        all_nodes = cmds.ls("*_planes*", type="transform") or []
        groups = [n for n in all_nodes if not n.endswith("_align") and not n.endswith("_perp")]
        for group in groups:
            align = f"{group}_align"
            perp = f"{group}_perp"
            if not cmds.objExists(align) or not cmds.objExists(perp):
                continue
            self.dropdown.blockSignals(True)
            self.dropdown.addItem(group)
            self.dropdown.blockSignals(False)
        if self.dropdown.count() > 0:
            self.dropdown.setCurrentIndex(0)
            self.on_group_changed()
            self.delete_btn.setEnabled(True)

    def align_joints(self):
        selected = cmds.ls(selection=True, type='joint', long=True)
        if len(selected) != 2:
            cmds.warning("Please select exactly two joints.")
            return

        start_joint, end_joint = selected[0], selected[1]
        start_pos = om.MVector(*cmds.xform(start_joint, query=True, worldSpace=True, translation=True))
        end_pos = om.MVector(*cmds.xform(end_joint, query=True, worldSpace=True, translation=True))
        end_joint_world_pos = cmds.xform(end_joint, query=True, worldSpace=True, translation=True)

        in_between = []
        current = start_joint
        while True:
            children = cmds.listRelatives(current, children=True, type='joint', fullPath=True)
            if not children:
                cmds.warning("Could not find a chain between the two selected joints.")
                return
            current = children[0]
            if current == end_joint:
                break
            in_between.append(current)

        view = self.view_btn_group.checkedId()
        if view == 0:
            ignore_axis = 2
        elif view == 1:
            ignore_axis = 0
        else:
            ignore_axis = 1

        for joint in in_between:
            joint_pos = om.MVector(*cmds.xform(joint, query=True, worldSpace=True, translation=True))

            # MORE MATH but this is to find the closest point for the joint to move to
            chain_vec = end_pos - start_pos
            projection_t = ((joint_pos - start_pos) * chain_vec) / (chain_vec * chain_vec)
            projected = start_pos + chain_vec * projection_t
            
            new_pos = [projected.x, projected.y, projected.z]
            new_pos[ignore_axis] = [joint_pos.x, joint_pos.y, joint_pos.z][ignore_axis]
            cmds.xform(joint, worldSpace=True, translation=new_pos)

        cmds.xform(end_joint, worldSpace=True, translation=end_joint_world_pos)

    def on_group_changed(self):
        name = self.dropdown.currentText()
        if not name:
            self._set_controls_enabled(False)
            return

        align = f"{name}_align"
        perp = f"{name}_perp"
        if not cmds.objExists(name) or not cmds.objExists(align) or not cmds.objExists(perp):
            self._set_controls_enabled(False)
            return

        history_align = [n for n in cmds.listHistory(align) or [] if cmds.nodeType(n) == "polyPlane"]
        history_perp = [n for n in cmds.listHistory(perp) or [] if cmds.nodeType(n) == "polyPlane"]
        if not history_align or not history_perp:
            self._set_controls_enabled(False)
            return

        self._set_controls_enabled(True)
        self.toggle_align_btn.setChecked(not cmds.getAttr(f"{align}.visibility"))
        self.toggle_align_btn.setText("Show" if self.toggle_align_btn.isChecked() else "Hide")
        self.toggle_perp_btn.setChecked(not cmds.getAttr(f"{perp}.visibility"))
        self.toggle_perp_btn.setText("Show" if self.toggle_perp_btn.isChecked() else "Hide")

        widgets = [self.sub_align_width, self.sub_align_height, self.sub_perp_width, self.sub_perp_height, self.width_align, self.width_perp]
        for w in widgets:
            w.blockSignals(True)

        self.sub_align_width.setValue(cmds.getAttr(f"{history_align[0]}.subdivisionsWidth"))
        self.sub_align_height.setValue(cmds.getAttr(f"{history_align[0]}.subdivisionsHeight"))
        self.sub_perp_width.setValue(cmds.getAttr(f"{history_perp[0]}.subdivisionsWidth"))
        self.sub_perp_height.setValue(cmds.getAttr(f"{history_perp[0]}.subdivisionsHeight"))
        self.width_align.setValue(cmds.getAttr(f"{history_align[0]}.width") / 2)
        self.width_perp.setValue(cmds.getAttr(f"{history_perp[0]}.width") / 2)

        for w in widgets:
            w.blockSignals(False)

    def _on_node_removed(self, node, client_data):
        try:
            if not self.isVisible():
                return
            name = om.MFnDependencyNode(node).name()
            idx = self.dropdown.findText(name)
            if idx >= 0:
                self.dropdown.blockSignals(True)
                self.dropdown.removeItem(idx)
                self.dropdown.blockSignals(False)
                if self.dropdown.count() == 0:
                    self._set_controls_enabled(False)
                    self.delete_btn.setEnabled(False)
                else:
                    self.on_group_changed()
        except Exception:
            pass

    def closeEvent(self, event):
        om.MMessage.removeCallback(self._node_removed_cb)
        super().closeEvent(event)

    def _set_controls_enabled(self, enabled):
        widgets = [self.toggle_align_btn, self.toggle_perp_btn, self.flip_align_btn, self.flip_perp_btn, self.sub_align_width, self.sub_align_height, self.sub_perp_width, self.sub_perp_height, self.width_align, self.width_perp]
        for w in widgets:
            w.setEnabled(enabled)

    def delete_group(self):
        name = self.dropdown.currentText()
        if not name:
            return
        om.MMessage.removeCallback(self._node_removed_cb)
        if cmds.objExists(name):
            cmds.delete(name)
        self.dropdown.blockSignals(True)
        self.dropdown.removeItem(self.dropdown.currentIndex())
        self.dropdown.blockSignals(False)
        if self.dropdown.count() == 0:
            self._set_controls_enabled(False)
            self.delete_btn.setEnabled(False)
        else:
            self.on_group_changed()

        self._node_removed_cb = om.MDGMessage.addNodeRemovedCallback(self._on_node_removed, "transform")

    def current_data(self):
        name = self.dropdown.currentText()
        if not name or not cmds.objExists(name):
            return None
        align = f"{name}_align"
        perp = f"{name}_perp"
        if not cmds.objExists(align) or not cmds.objExists(perp):
            return None
        history_align = [n for n in cmds.listHistory(align) or [] if cmds.nodeType(n) == "polyPlane"]
        history_perp = [n for n in cmds.listHistory(perp) or [] if cmds.nodeType(n) == "polyPlane"]
        if not history_align or not history_perp:
            return None
        return {
            "group": name,
            "plane_align": align,
            "node_align": history_align[0],
            "plane_perp": perp,
            "node_perp": history_perp[0],
        }

    def update_subdivisions(self, plane_type):
        data = self.current_data()
        if data and cmds.objExists(data[f"node_{plane_type}"]):
            width = getattr(self, f"sub_{plane_type}_width").value()
            height = getattr(self, f"sub_{plane_type}_height").value()
            cmds.setAttr(f"{data[f'node_{plane_type}']}.subdivisionsWidth", width)
            cmds.setAttr(f"{data[f'node_{plane_type}']}.subdivisionsHeight", height)

    def update_plane_width(self, plane_type):
        data = self.current_data()
        if not data or not cmds.objExists(data[f"node_{plane_type}"]):
            return
        width = getattr(self, f"width_{plane_type}").value()
        cmds.setAttr(f"{data[f'node_{plane_type}']}.width", width * 2)

    def flip_normal(self, plane_type):
        data = self.current_data()
        if data and cmds.objExists(data[f"plane_{plane_type}"]):
            cmds.polyNormal(data[f"plane_{plane_type}"], normalMode=0, userNormalMode=0, ch=1)

    def toggle_plane(self, plane_type, checked):
        data = self.current_data()
        if data and cmds.objExists(data[f"plane_{plane_type}"]):
            cmds.setAttr(f"{data[f'plane_{plane_type}']}.visibility", not checked)
            btn = getattr(self, f"toggle_{plane_type}_btn")
            btn.setText("Show" if checked else "Hide")