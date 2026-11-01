import maya.cmds as cmds
import maya.mel as mel
import math

try:
    from PySide6 import QtWidgets, QtGui
    from shiboken6 import wrapInstance
    import maya.OpenMayaUI as omui
except:
    from PySide2 import QtWidgets, QtGui
    from shiboken2 import wrapInstance
    import maya.OpenMayaUI as omui

def maya_main_window():
    ptr = omui.MQtUtil.mainWindow()
    return wrapInstance(int(ptr), QtWidgets.QWidget) if ptr else None

def get_total_distance_moved(start_frame, end_frame):
    selection = cmds.ls(selection=True, long=True) or []
    if not selection:
        return None

    item = selection[0]

    if end_frame < start_frame:
        start_frame, end_frame = end_frame, start_frame

    current_frame = cmds.currentTime(query=True)
    cmds.refresh(suspend=True)
    try:
        cmds.currentTime(start_frame, edit=True, update=True)
        prev = cmds.xform(item, query=True, worldSpace=True, translation=True)
        total = 0.0

        for frame in range(start_frame + 1, end_frame + 1):
            cmds.currentTime(frame, edit=True, update=True)
            pos = cmds.xform(item, query=True, worldSpace=True, translation=True)
            dx = pos[0] - prev[0]
            dy = pos[1] - prev[1]
            dz = pos[2] - prev[2]
            total += math.sqrt(dx * dx + dy * dy + dz * dz)
            prev = pos
    finally:
        cmds.currentTime(current_frame, edit=True, update=True)
        cmds.refresh(suspend=False)
        cmds.refresh()
    return total

def get_unit_scale():
    unit = cmds.currentUnit(query=True, linear=True)
    unit_scale_map={"mm": 0.001, "cm": 0.01, "m": 1.0, "in": 0.0254, "ft": 0.3048, "yd": 0.9144, "": 0.01}
    return unit_scale_map.get(unit, 0.01)


class SpeedometerDialog(QtWidgets.QDialog):

    window = None

    @classmethod
    def show_dialog(cls):
        if cls.window is not None:
            cls.window.close()
        cls.window = SpeedometerDialog(parent=maya_main_window())
        cls.window.show()

    def __init__(self, parent=None):
        super(SpeedometerDialog, self).__init__(parent)
        self.setWindowTitle("Speedometer")

        self.start_input = QtWidgets.QLineEdit()
        self.end_input = QtWidgets.QLineEdit()
        input_font = QtGui.QFont()
        input_font.setPointSize(12)
        self.start_input.setFont(input_font)
        self.end_input.setFont(input_font)
        self.start_input.setValidator(QtGui.QIntValidator())
        self.end_input.setValidator(QtGui.QIntValidator())
        self.start_input.setPlaceholderText("Start frame")
        self.end_input.setPlaceholderText("End frame")

        linear_unit = cmds.currentUnit(query=True, linear=True)
        self.unit_label = QtWidgets.QLabel(f"Scene Unit: {linear_unit}")
        self.distance_label = QtWidgets.QLabel("Distance: -")
        self.duration_label = QtWidgets.QLabel("Duration: -")
        self.speed_label = QtWidgets.QLabel("Speed: -")
        label_font = QtGui.QFont()
        label_font.setPointSize(12)
        self.unit_label.setFont(label_font)
        self.distance_label.setFont(label_font)
        self.duration_label.setFont(label_font)
        self.speed_label.setFont(label_font)
        self.button = QtWidgets.QPushButton("Calculate")
        self.button.clicked.connect(self.calculate)
        self.start_input.returnPressed.connect(self.end_input.setFocus)
        self.end_input.returnPressed.connect(self.calculate)

        self.timeline_button = QtWidgets.QPushButton("Use Timeline Selection")
        self.timeline_button.clicked.connect(self.use_timeline_selection)

        self.playback_button = QtWidgets.QPushButton("Use Playback Range")
        self.playback_button.clicked.connect(self.use_playback_range)

        self.shot_button = QtWidgets.QPushButton("Use Shot Range")
        self.shot_button.clicked.connect(self.use_selected_shot_range)

        layout = QtWidgets.QVBoxLayout(self)
        input_row = QtWidgets.QHBoxLayout()
        input_row.addWidget(self.start_input)
        input_row.addWidget(self.end_input)
        layout.addLayout(input_row)

        button_row = QtWidgets.QHBoxLayout()
        button_row.addWidget(self.timeline_button)
        button_row.addWidget(self.playback_button)
        button_row.addWidget(self.shot_button)
        layout.addLayout(button_row)
        layout.addWidget(self.unit_label)
        layout.addWidget(self.distance_label)
        layout.addWidget(self.duration_label)
        layout.addWidget(self.speed_label)
        layout.addWidget(self.button)

    def use_timeline_selection(self):
        try:
            slider = mel.eval('$tmpVar=$gPlayBackSlider')
            start, end = cmds.timeControl(slider, query=True, rangeArray=True)
            self.start_input.setText(str(int(start)))
            self.end_input.setText(str(int(end)))
        except Exception:
            pass 

    def use_playback_range(self):
        try:
            start = int(cmds.playbackOptions(query=True, animationStartTime=True))
            end = int(cmds.playbackOptions(query=True, animationEndTime=True))
            self.start_input.setText(str(start))
            self.end_input.setText(str(end))
        except Exception:
            pass 

    def use_selected_shot_range(self):
        try:
            shots = cmds.ls(selection=True, type="shot") or []
            if not shots:
                cmds.warning("Select a shot in Camera Sequencer first.")
                return
            
            shot = shots[0]
            start = int(round(cmds.getAttr(f"{shot}.startFrame")))
            end = int(round(cmds.getAttr(f"{shot}.endFrame")))

            print(f"Shot: {shot}, Start: {start}, End: {end}")

            self.start_input.setText(str(start))
            self.end_input.setText(str(end))
        except Exception as exc:
            cmds.warning(f"Failed to get shot range: {exc}")

    def calculate(self):
        start_value = self.start_input.text()
        end_value = self.end_input.text()

        self.start_input.setStyleSheet("")
        self.end_input.setStyleSheet("")

        is_empty = False
        if not start_value:
            self.start_input.setStyleSheet("border: 1px solid #e74c3c;")
            is_empty = True
        if not end_value:
            self.end_input.setStyleSheet("border: 1px solid #e74c3c;")
            is_empty = True
        if is_empty:
            self.distance_label.setText("Distance: -")
            self.duration_label.setText("Duration: -")
            self.speed_label.setText("Speed: -")
            return

        selection = cmds.ls(selection=True, long=True) or []
        if not selection:
            cmds.warning("Please select an object first.")
            self.distance_label.setText("Distance: -")
            self.duration_label.setText("Duration: -")
            self.speed_label.setText("Speed: -")
            return

        start_frame = int(start_value)
        end_frame = int(end_value)

        distance = get_total_distance_moved(start_frame, end_frame)
        duration = abs(end_frame - start_frame)
        speed = (distance / duration) if (distance is not None and duration > 0) else 0.0
        if distance is None or speed is None:
            self.distance_label.setText("Distance: None")
            self.duration_label.setText("Duration: None")
            self.speed_label.setText("Speed: None")
        else:
            scale = get_unit_scale()
            distance_m = distance * scale
            speed_m = speed * scale
            time_unit = cmds.currentUnit(query=True, time=True)
            fps_map = {"game": 15.0, "film": 24.0, "pal": 25.0, "ntsc": 30.0, "show": 48.0, "palf": 50.0, "ntscf": 60.0}
            fps = fps_map.get(time_unit, 24.0)
            duration_seconds = duration / fps if fps else 0
            speed_mps = speed_m * fps
            speed_kmh = speed_mps * 3.6
            self.distance_label.setText(f"Distance: {distance_m:.3f} m")
            self.duration_label.setText(f"Duration: {duration_seconds:.2f} s")
            self.speed_label.setText(f"Speed: {speed_mps:.3f} m/s | {speed_kmh:.3f} km/h")
