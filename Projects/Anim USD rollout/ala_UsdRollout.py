import os
import shutil
import subprocess
import glob
import re
from pathlib import Path
import sgtk
import maya.OpenMayaUI as omui
import maya.cmds as cmds

try:
    from PySide6 import QtWidgets, QtCore
    from shiboken6 import wrapInstance
except ImportError:
    from PySide2 import QtWidgets, QtCore
    from shiboken2 import wrapInstance

import shotgun_utils
import maya_pipeline.utils as maya_utils

MAX_PARALLEL_SHOTS = 10


def maya_main_window():
    ptr = omui.MQtUtil.mainWindow()
    if ptr:
        return wrapInstance(int(ptr), QtWidgets.QWidget)
    return None

class VariableSelectionDialog(QtWidgets.QDialog):
    dlg_instance = None
    
    @classmethod
    def show_dialog(cls, asset, shots_info):
        if cls.dlg_instance:
            cls.dlg_instance.close()
            cls.dlg_instance.deleteLater()

        cls.dlg_instance = VariableSelectionDialog(asset, shots_info)
        cls.dlg_instance.show()

    def __init__(self, asset, shots_info, parent=maya_main_window()):
        super(VariableSelectionDialog, self).__init__(parent)

        self.asset = asset
        self.shots_info = shots_info
        self.checkboxes = {}
        self.sequence_checkboxes = {}
        self.sequence_shots = {}
        self.shot_to_sequence = {}

        self.setWindowTitle("Variable Selection")
        self.resize(600, 600)

        self.build_ui()

    def build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)

        main_label = QtWidgets.QLabel(f"Asset: {self.asset}")
        font = main_label.font()
        font.setBold(True)
        main_label.setFont(font)

        layout.addWidget(main_label)
        layout.addWidget(QtWidgets.QLabel("Select related shots:"))

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)

        container = QtWidgets.QWidget()
        checkbox_layout = QtWidgets.QVBoxLayout(container)

        # Select All
        self.all_checkbox = QtWidgets.QCheckBox("All Shots")
        self.all_checkbox.stateChanged.connect(self.toggle_all)

        layout.addWidget(self.all_checkbox)

        sequence_map = {}

        for shot_info in self.shots_info:
            seq = shot_info.get("sg_sequence.Sequence.code") or "NO_SEQ"
            sequence_map.setdefault(seq, []).append(shot_info)

        for sequence, sequence_shots in sorted(sequence_map.items()):
            seq_cb = QtWidgets.QCheckBox(sequence)
            seq_font = seq_cb.font()
            seq_font.setBold(True)
            seq_cb.setFont(seq_font)

            checkbox_layout.addWidget(seq_cb)

            self.sequence_checkboxes[sequence] = seq_cb
            self.sequence_shots[sequence] = []

            seq_cb.stateChanged.connect(
                lambda state, seq=sequence: self.toggle_sequence(seq, state)
            )

            for shot_info in sequence_shots:
                row = QtWidgets.QWidget()
                row_layout = QtWidgets.QHBoxLayout(row)

                cb = QtWidgets.QCheckBox()

                self.checkboxes[shot_info["code"]] = cb
                self.sequence_shots[sequence].append(cb)
                self.shot_to_sequence[cb] = sequence

                cb.toggled.connect(
                    lambda checked, seq=sequence: self.update_sequence_checkbox(seq)
                )

                code_label = QtWidgets.QLabel(shot_info["code"])
                code_label.setMinimumWidth(100)
                code_label.setAlignment(QtCore.Qt.AlignCenter)

                desc_label = QtWidgets.QLabel(
                    shot_info.get("description", "")
                )
                desc_label.setWordWrap(True)

                row_layout.addWidget(cb)
                row_layout.addWidget(code_label)
                row_layout.addWidget(desc_label, 1)

                checkbox_layout.addWidget(row)

        checkbox_layout.addStretch()

        scroll.setWidget(container)
        layout.addWidget(scroll)

        gather_btn = QtWidgets.QPushButton("Update Shots")
        gather_btn.clicked.connect(self.update_shots)

        layout.addWidget(gather_btn)

    def update_shots(self):
        selected = [var for var, cb in self.checkboxes.items()if cb.isChecked()]

        print("Main Rig:", self.asset)
        print("Selected Shots:", selected)

        if not selected:
            QtWidgets.QMessageBox.information(self, "USD Rollout", "No shots selected.")
            return

        message = (
            "Updating multiple shots can be memory intensive. "
            "It is highly recommended to close other open apps prior to updating."
        )
        answer = QtWidgets.QMessageBox.question(
            self, "USD Rollout", message,
            QtWidgets.QMessageBox.Ok | QtWidgets.QMessageBox.Cancel
        )
        if answer != QtWidgets.QMessageBox.Ok:
            return

        mayapy =os.path.join(os.environ["MAYA_LOCATION"], "bin", "mayapy")
        script = os.path.join(os.environ["REZ_MAYA_PIPELINE_ROOT"], "bin", "anim_usd_rollout.py")
        log_dir = os.path.expanduser("~/usd_rollout_logs/%s" % self.asset)
        os.makedirs(log_dir, exist_ok=True)

        commands = [
            "%s %s -s %s -a %s > %s 2>&1" % (mayapy, script, shot, self.asset, os.path.join(log_dir, "%s.log" % shot))
            for shot in selected
        ]

        batches = [commands[i:i + MAX_PARALLEL_SHOTS] for i in range(0, len(commands), MAX_PARALLEL_SHOTS)]
        shot_runs = " ; ".join("%s & wait" % " & ".join(batch) for batch in batches)

        # Post a Flow note with every shot's result, addressed to whoever ran the tool
        user = sgtk.util.get_current_user(shotgun_utils.get_tk())
        notify = "%s %s -a %s --notify-log-dir %s --notify-shots %s" % (mayapy, script, self.asset, log_dir, " ".join(selected))
        if user:
            notify += " --notify-user %d" % user["id"]

        # Remove last run's logs so the progress window doesn't show old results
        for shot in selected:
            old_log = os.path.join(log_dir, "%s.log" % shot)
            if os.path.exists(old_log):
                os.remove(old_log)

        print("Running headless, logs: %s" % log_dir)

        with open(os.path.join(log_dir, "summary.log"), "w") as log:
            subprocess.Popen(["bash", "-c", "%s ; %s" % (shot_runs, notify)], stdout=log, stderr=subprocess.STDOUT)

        self.progress = RolloutProgressDialog(self.asset, selected, log_dir)
        self.progress.show()

    def toggle_all(self, state):
        checked = bool(state)

        for cb in self.checkboxes.values():
            cb.setChecked(checked)

        for cb in self.sequence_checkboxes.values():
            cb.setChecked(checked)

    def toggle_sequence(self, sequence, state):
        checked = bool(state)

        for cb in self.sequence_shots[sequence]:
            cb.setChecked(checked)

    def update_all_checkbox(self):
        all_checked = all(
            cb.isChecked()
            for cb in self.sequence_checkboxes.values()
        )

        self.all_checkbox.blockSignals(True)
        self.all_checkbox.setChecked(all_checked)
        self.all_checkbox.blockSignals(False)

    def update_sequence_checkbox(self, sequence):
        seq_cb = self.sequence_checkboxes[sequence]
        shot_cbs = self.sequence_shots[sequence]

        all_checked = all(cb.isChecked() for cb in shot_cbs)

        seq_cb.blockSignals(True)
        seq_cb.setChecked(all_checked)
        seq_cb.blockSignals(False)

        self.update_all_checkbox()

class RolloutProgressDialog(QtWidgets.QDialog):
    def __init__(self, asset, shots, log_dir, parent=maya_main_window()):
        super(RolloutProgressDialog, self).__init__(parent)

        self.shots = shots
        self.log_dir = log_dir

        self.setWindowTitle("USD Rollout: %s" % asset)
        self.resize(500, 400)

        layout = QtWidgets.QVBoxLayout(self)
        self.summary_label = QtWidgets.QLabel()
        self.shot_list = QtWidgets.QListWidget()
        layout.addWidget(self.summary_label)
        layout.addWidget(self.shot_list)
        layout.addWidget(QtWidgets.QLabel("Keep Maya open until the rollout finishes."))

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(5000)
        self.refresh()

    def refresh(self):
        self.shot_list.clear()
        done = 0
        for shot in self.shots:
            log_path = os.path.join(self.log_dir, "%s.log" % shot)
            if not os.path.exists(log_path):
                status = "Waiting"
            else:
                status = read_shot_result(log_path) or "Running"
            if status not in ("Waiting", "Running"):
                done += 1
            self.shot_list.addItem("%s: %s" % (shot, status))

        self.summary_label.setText("%d / %d shots done" % (done, len(self.shots)))
        if done == len(self.shots):
            self.summary_label.setText("All done. Flow note will arrive shortly.")
            self.timer.stop()

# ---------------------------------- UI ----------------------------------------

# ------------------------- Getting ShotGrid info ------------------------------

def get_asset_name():
    context = sgtk.platform.current_engine().context

    if not context.entity or context.entity["type"] != "Asset":
        raise RuntimeError(
            "Current context is not an Asset. Open the rig scene through Flow first."
        )

    return context.entity["name"]

def find_all_dependent_shots(asset):
    sg = shotgun_utils.get_tk().shotgun
    project_id = shotgun_utils.get_project_id()

    asset_id = sg.find_one(
        "Asset",
        [["code", "is", asset], ['project.Project.id', 'is', project_id]],
        ["id"]
    )

    shots_info = sg.find(
        "Shot",
        [["assets", "is", asset_id], ['project.Project.id', 'is', project_id]],
        ["code", "description", "sg_sequence.Sequence.code"]
    )

    return shots_info

# ------------------------- Getting ShotGrid info ------------------------------

# -------------------------- Updating Maya scene -------------------------------


def print_dialog_message(*args, **kwargs):
    print("%s\n%s" % (kwargs.get("title", ""), kwargs.get("message", "")))


def open_next_version(shot):
    tk = shotgun_utils.get_tk()

    root_template = "wip_shot_task_root"
    root_template_path = tk.templates[root_template]

    fields = {}
    fields['Shot'] = shot
    fields['Sequence'] = shot.split("_")[0]
    fields['Step'] = "animation"
    fields['Task'] = "animation"

    base_path = root_template_path.apply_fields(fields)

    latest_version, latest_extension, first_name, last_name = get_latest_version_info(base_path, shot)
    fields['version'] = latest_version
    fields['maya_extension'] = latest_extension
    fields['HumanUser.firstname'] = first_name
    fields['HumanUser.lastname'] = last_name

    template = "maya_wip_shot_build_path"
    template_path = tk.templates[template]
    latest_version_path = template_path.apply_fields(fields)

    print("Latest file path: ", latest_version_path)

    # Save maya scene
    fields['version'] = latest_version + 1
    next_version_path = template_path.apply_fields(fields)

    shutil.copy(latest_version_path, next_version_path)

    original_confirm_dialog = cmds.confirmDialog
    cmds.confirmDialog = print_dialog_message
    try:
        cmds.file(next_version_path, open=True, force=True)
    finally:
        cmds.confirmDialog = original_confirm_dialog

    return next_version_path

def update_rig(asset):
    tank = shotgun_utils.get_tk()
    found = False

    for rig_file_path, ref_node, _ in maya_utils.get_refs():
        template_path = tank.template_from_path(rig_file_path)
        if not template_path or template_path.get_fields(rig_file_path).get("Asset") != asset:
            continue

        found = True
        latest_version = maya_utils.get_latest_rig(rig_file_path, template_path, tank)
        if latest_version and latest_version != rig_file_path:
            print("Updating %s: %s -> %s" % (asset, rig_file_path, latest_version))
            cmds.file(latest_version, loadReference=ref_node)
        else:
            print("%s already latest: %s" % (asset, rig_file_path))

    if not found:
        raise RuntimeError("No reference to %s found in the shot" % asset)

    maya_utils.loadAllReferences(cmds.ls(rf=True))

def get_latest_version_info(sequence_path, shot):
    pattern = os.path.join(
        sequence_path,
        "*",
        "maya",
        "build",
        f"{shot}_animation_animation.v*.m[ab]",
    )

    files = glob.glob(pattern)

    if not files:
        raise RuntimeError("No animation work file found for %s in %s" % (shot, sequence_path))

    latest_file = max(files, key=get_version_num)

    latest_version = get_version_num(latest_file)
    latest_extension = Path(latest_file).suffix

    first_name, last_name = get_name(latest_file)

    return latest_version, latest_extension, first_name, last_name

def get_version_num(path):
    match = re.search(
        r'v(\d+)\.m[ab]$',
        Path(path).name
    )

    if not match:
        # print(f"Improper naming convention for path: {path}")
        return 1

    return int(match.group(1))

def get_name(path):
    parts = Path(path).parts

    maya_index = parts.index("maya")

    full_name = parts[maya_index - 1]

    first_name = full_name.split(".")[0]
    last_name = full_name.split(".")[1]

    return first_name, last_name

USD_PLUGIN = "Publish USD Cache"
ABC_PLUGIN = "Publish Alembic Cache"


def validate_animcaches(usd=True, abc=True, frame_hold=False):
    engine = sgtk.platform.current_engine()
    app = engine.apps["tk-multi-publish2"]

    cmds.file(save=True, force=True)

    manager = app.create_publish_manager()
    manager.collect_session()

    wanted = set()
    if usd: 
        wanted.add(USD_PLUGIN)
    if abc: 
        wanted.add(ABC_PLUGIN)

    for item in manager.tree:
        item.description = "Updated rig to latest"

        if item.type_spec == "maya.session":
            for task in item.tasks:
                task.active = True
            continue

        is_cache = item.type_spec == "maya.session.geometry.animcache"
        for task in item.tasks:
            name = task.plugin.name
            task.active = is_cache and name in wanted
            if task.active and name == USD_PLUGIN:
                task.settings["Frame Hold"].value = frame_hold

    failed = manager.validate()
    if failed:
        raise RuntimeError("Validation failed: %s" % failed)

    return manager

# -------------------------- Updating Maya scene -------------------------------

# ------------------------- Posting results to Flow ----------------------------

def read_shot_result(log_path):
    if not os.path.exists(log_path):
        return None

    result = None
    with open(log_path, errors="replace") as log:
        for line in log:
            if line.startswith("[RESULT]"):
                result = line[len("[RESULT]"):].strip()

    return result

def post_summary_note(asset, shots, log_dir, user_id):
    sg = shotgun_utils.get_tk().shotgun
    project_id = shotgun_utils.get_project_id()

    lines = []
    for shot in shots:
        log_path = os.path.join(log_dir, "%s.log" % shot)
        lines.append("%s: %s" % (shot, read_shot_result(log_path) or "no result, check %s" % log_path))

    asset_entity = sg.find_one(
        "Asset",
        [["code", "is", asset], ["project.Project.id", "is", project_id]],
        ["id"]
    )

    note = {
        "project": {"type": "Project", "id": project_id},
        "subject": "USD Rollout: %s" % asset,
        "content": "USD Rollout: %s (%d shots)\n\n%s" % (asset, len(shots), "\n".join(lines)),
        "note_links": [asset_entity] if asset_entity else [],
    }
    if user_id:
        note["addressings_to"] = [{"type": "HumanUser", "id": user_id}]

    note = sg.create("Note", note)
    print("Posted note %d:\n%s" % (note["id"], "\n".join(lines)))

# ------------------------- Posting results to Flow ----------------------------

def main():
    asset = get_asset_name()

    shots_info = find_all_dependent_shots(asset)

    VariableSelectionDialog.show_dialog(asset, shots_info)

if __name__ == "__main__":
    main()