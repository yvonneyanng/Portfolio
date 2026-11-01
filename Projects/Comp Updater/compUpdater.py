import os
import glob
import re
import shutil
from pathlib import Path
import nuke
import shotgun_utils
from nukescripts import panels
from turret import resolver
from nuke_pipeline import create_comp_update_jobs

if nuke.NUKE_VERSION_MAJOR < 16:
    from PySide2 import QtCore, QtWidgets
else:
    from PySide6 import QtCore, QtWidgets

CAMERA_TEMPLATES = ["maya_publish_shot_asset_cache_usd", "maya_publish_shot_asset_cache_alembic"]


def shotNameSortKey(text):
    return [int(part) if part.isdigit() else part for part in re.split(r'(\d+)', text)]


def findShotsBySequence(tk):
    project_id = shotgun_utils.get_project_id()
    shots = tk.shotgun.find(
        'Shot',
        [['project', 'is', {'type': 'Project', 'id': project_id}]],
        ['code', 'sg_sequence'],
    )

    shots_by_seq = {}
    for shot in shots:
        seq = shot['sg_sequence']
        if not shot['code'] or not seq:
            continue
        if shot['code'] == seq['name'] + '_seq':
            continue
        shots_by_seq.setdefault(seq['name'], []).append(shot['code'])

    return {
        seq: sorted(shot_codes, key=shotNameSortKey)
        for seq, shot_codes in sorted(shots_by_seq.items(), key=lambda item: shotNameSortKey(item[0]))
    }


def find_latest_workfile(shot, artist):
    tk = shotgun_utils.get_tk()

    fields = {}
    fields['Shot'] = shot
    fields['Sequence'] = shot.split("_")[0]
    fields['Step'] = "comp"
    fields['Task'] = "comp"

    base_path = tk.templates["wip_shot_task_root"].apply_fields(fields)

    return get_latest_version_info(base_path, shot, artist)


def get_latest_version_info(sequence_path, shot, artist):
    pattern = os.path.join(
        sequence_path,
        "*",
        "nuke",
        "build",
        f"{shot}_comp_comp.v*.nk",
    )

    files = [path for path in glob.glob(pattern) if Path(path).parts[-4].lower() == artist.lower()]

    if not files:
        raise RuntimeError("No comp work file found for %s by %s in %s" % (shot, artist, sequence_path))

    latest_file = max(files, key=get_version_num)

    latest_version = get_version_num(latest_file)

    first_name, last_name = get_name(latest_file)

    return latest_file, latest_version, first_name, last_name


def get_next_version_path(latest_path):
    template = shotgun_utils.get_tk().templates["nuke_wip_shot_build_path"]

    fields = template.get_fields(latest_path)
    fields['version'] += 1

    next_path = template.apply_fields(fields)

    if os.path.exists(next_path):
        raise RuntimeError("Next version already exists: %s" % next_path)

    return next_path


def open_next_version(latest_path):
    next_path = get_next_version_path(latest_path)

    shutil.copy(latest_path, next_path)

    nuke.scriptOpen(next_path)

    return next_path


def find_shot_camera(shot):
    tk = shotgun_utils.get_tk()

    for node in nuke.allNodes():
        if not node.Class().startswith("Camera") or not node.knob("file"):
            continue

        path = node["file"].value()
        if not path:
            continue

        template = tk.template_from_path(path)
        if not template or template.name not in CAMERA_TEMPLATES:
            continue

        if template.get_fields(path).get("Asset") != shot + "_cam":
            continue

        return node, template.name

    return None, None


def find_latest_camera_path(shot, template_name):
    query = "tank:/{0}/{1}?Task=layout&Shot={2}&Sequence={3}&Step=layout&version=latest&Asset={2}_cam&asset_type=camera".format(
        shotgun_utils.get_project_code(), template_name, shot, shot.split("_")[0]
    )

    try:
        path = resolver.uri_to_filepath(query)
    except Exception as e:
        print("Resolve failed:", e)
        return None

    return path if path and os.path.exists(path) else None


def update_camera(shot):
    node, template_name = find_shot_camera(shot)
    if not node:
        return None

    old_path = node["file"].value()

    latest_path = find_latest_camera_path(shot, template_name)
    if not latest_path:
        print("No latest camera found for %s, leaving it as is" % node.name())
        latest_path = old_path

    if latest_path != old_path:
        node["file"].setValue(latest_path)
        if node.knob("Reload"):
            node["Reload"].execute()

    return node.name(), old_path, latest_path


def find_comp_write_node():
    all_write_tank_nodes = nuke.allNodes("WriteTank")
    write_tank_nodes = [node for node in all_write_tank_nodes if not (node.knob("disable") and node["disable"].value())]

    disabled = [node.name() for node in all_write_tank_nodes if node not in write_tank_nodes]
    if disabled:
        print("Ignoring disabled WriteTanks: %s" % disabled)

    for node in write_tank_nodes:
        if node.knob("comp_role") and node["comp_role"].value() == "comp_output":
            print("Found comp WriteTank by comp_role tag")
            return node

    if not write_tank_nodes:
        raise RuntimeError("No enabled WriteTank found in the script")

    print("No comp_output tag found, using the first WriteTank: %s" % [node.name() for node in write_tank_nodes])
    return write_tank_nodes[0]


def tag_comp_write_node(node):
    if not node.knob("comp_role"):
        node.addKnob(nuke.String_Knob("comp_role", "comp_role"))
        node["comp_role"].setVisible(False)
    node["comp_role"].setValue("comp_output")
    print("Tagged %s as comp_output" % node.name())


def get_render_settings():
    node = find_comp_write_node()

    render_path = node['path_context'].value() + os.sep + node['path_local'].value()
    if os.path.isdir(render_path):
        raise RuntimeError("Render for this version already exists: %s" % render_path)

    frames = "%d-%d" % (nuke.root().firstFrame(), nuke.root().lastFrame())

    return node, render_path, frames


def get_version_num(path):
    match = re.search(r'v(\d+)\.nk$', Path(path).name)

    if not match:
        return 1

    return int(match.group(1))


def get_name(path):
    parts = Path(path).parts

    nuke_index = parts.index("nuke")

    full_name = parts[nuke_index - 1]

    first_name = full_name.split(".")[0]
    last_name = full_name.split(".")[1]

    return first_name, last_name


class CompUpdater(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.tk = shotgun_utils.get_tk()
        self.makeUI()
        self.populateShots()

    def makeUI(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(QtWidgets.QLabel("Select the shots you want to update:"))

        self.shotTree = QtWidgets.QTreeWidget()
        self.shotTree.setHeaderHidden(True)
        layout.addWidget(self.shotTree)

        self.updateButton = QtWidgets.QPushButton("Update Selected Shots")
        self.updateButton.clicked.connect(self.onUpdate)
        layout.addWidget(self.updateButton)

    def populateShots(self):
        self.shotTree.clear()
        for seq, shot_codes in findShotsBySequence(self.tk).items():
            seq_item = QtWidgets.QTreeWidgetItem(self.shotTree, [seq])
            seq_item.setFlags(seq_item.flags() | QtCore.Qt.ItemIsAutoTristate)
            seq_item.setCheckState(0, QtCore.Qt.Unchecked)
            for shot_code in shot_codes:
                shot_item = QtWidgets.QTreeWidgetItem(seq_item, [shot_code])
                shot_item.setCheckState(0, QtCore.Qt.Unchecked)
        self.shotTree.expandAll()

    def getSelectedShots(self):
        selected = []
        for seq_index in range(self.shotTree.topLevelItemCount()):
            seq_item = self.shotTree.topLevelItem(seq_index)
            for shot_index in range(seq_item.childCount()):
                shot_item = seq_item.child(shot_index)
                if shot_item.checkState(0) == QtCore.Qt.Checked:
                    selected.append(shot_item.text(0))
        return selected

    def onUpdate(self):
        shots = self.getSelectedShots()
        if not shots:
            nuke.message("Select at least one shot to update.")
            return

        if not nuke.ask("Update these %d shot(s)?\n\n%s" % (len(shots), "\n".join(shots))):
            return

        print("Comp Updater selected shots:", shots)
        create_comp_update_jobs.send_update_jobs(shots)
        # create_comp_update_jobs.run_update_jobs_locally(shots)

        nuke.message("Submitted %d shot(s) for update." % len(shots))


def compUpdater():
    nuke.CompUpdater = CompUpdater
    pane = nuke.getPaneFor('Properties.1')
    panels.registerWidgetAsPanel('nuke.CompUpdater', 'Comp Updater', 'ala.compUpdater', True).addToPane(pane)