import os
import pwd
import time
import traceback
import hou

try:
    from PySide6 import QtCore, QtWidgets
except ImportError:
    from PySide2 import QtCore, QtWidgets


_TEMPLATE_STAGING_PATH = os.environ.get("ALA_TEMPLATE_STAGING_PATH", "")
_TEMPLATES_FOLDER_NAME = "hda_templates"
_LOCK_EXTENSION = ".lock"
_LOCK_STALE_SECONDS = 8 * 60 * 60

def _get_templates_path():
    otlscan_path = os.environ.get("HOUDINI_OTLSCAN_PATH", "")
    for entry in otlscan_path.split(os.pathsep):
        if os.path.basename(os.path.normpath(entry)) == _TEMPLATES_FOLDER_NAME and os.path.isdir(entry):
            return entry
    return ""

_TEMPLATE_PATH = _get_templates_path()

def _version_sort_key(version):
    if version == "default":
        return (float("-inf"),)
    key = []
    for part in version.split("."):
        try:
            key.append(int(part))
        except ValueError:
            key.append(0)
    return tuple(key)

def _get_template_data(root_path, needs_install):
    data = {}

    if not root_path or not os.path.isdir(root_path):
        return data

    for filename in sorted(os.listdir(root_path)):
        if filename.endswith(_LOCK_EXTENSION):
            continue
        library_path = os.path.join(root_path, filename)
        if not os.path.isfile(library_path):
            continue

        try:
            if needs_install:
                hou.hda.installFile(library_path, force_use_assets=True)
            definitions = hou.hda.definitionsInFile(library_path)
        except Exception:
            continue

        for defn in definitions or []:
            node_type = defn.nodeType()
            if node_type is None:
                continue

            node_type_name = defn.nodeTypeName()
            components = node_type_name.split("::")
            base_name = components[-2] if len(components) >= 2 else components[-1]
            version = (components[-1] if len(components) >= 2 else "") or defn.version() or "default"

            if base_name not in data:
                data[base_name] = {
                    "versions": [],
                    "category": node_type.category(),
                }

            if version not in data[base_name]["versions"]:
                data[base_name]["versions"].append(version)

    for name in data:
        data[name]["versions"].sort(key=_version_sort_key, reverse=True)
    return dict(sorted(data.items()))

def _build_type_name(base_name, version):
    return "{}::{}".format(base_name, version) if version != "default" else base_name

def _resolve_defn(info, base_name, version):
    node_type = hou.nodeType(info["category"], _build_type_name(base_name, version))
    return node_type.definition() if node_type else None

def _get_new_version_num(version_str, component):
    parts = version_str.split(".")
    try:
        if len(parts) == 1:
            major = int(parts[0])
            return str(major + 1) if component == "major" else "{}.1".format(major)
        major, minor = int(parts[0]), int(parts[1])
        if component == "major":
            return "{}.0".format(major + 1)
        return "{}.{}".format(major, minor + 1)
    except (ValueError, IndexError):
        raise ValueError("Cannot parse version string: {!r}".format(version_str))

def _current_user():
    return pwd.getpwuid(os.getuid()).pw_name

def _lock_path_for(base_name):
    return os.path.join(_TEMPLATE_STAGING_PATH, base_name + _LOCK_EXTENSION)

def _read_lock(base_name):
    if not _TEMPLATE_STAGING_PATH:
        return None
    lock_path = _lock_path_for(base_name)
    if not os.path.isfile(lock_path):
        return None
    try:
        with open(lock_path, "r") as f:
            user, timestamp = f.read().strip().split("|")
        return {"user": user, "time": float(timestamp)}
    except Exception:
        return None

def _write_lock(base_name):
    if not _TEMPLATE_STAGING_PATH:
        return False
    lock_path = _lock_path_for(base_name)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o664)
    except OSError:
        return False
    try:
        os.write(fd, "{}|{}".format(_current_user(), time.time()).encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(lock_path, 0o664)
    except OSError:
        pass
    return True

def _clear_lock(base_name):
    if not _TEMPLATE_STAGING_PATH:
        return
    try:
        os.remove(_lock_path_for(base_name))
    except OSError:
        pass

def _lock_is_stale(lock_info):
    return (time.time() - lock_info["time"]) > _LOCK_STALE_SECONDS

def _increase_hda_version(node, version_type, save_to_path):
    defn = node.type().definition()
    if defn is None:
        raise RuntimeError("Node '{}' has no HDA definition.".format(node.path()))

    components = node.type().nameComponents()
    base_name = components[2]
    current_ver = components[3] or defn.version()

    if not current_ver:
        raise RuntimeError(
            "Cannot determine current version of '{}'.".format(base_name)
        )

    new_version = _get_new_version_num(current_ver, version_type)
    new_full_name = "{}::{}".format(base_name, new_version)
    menu_name = defn.description()

    save_dir = os.path.dirname(os.path.abspath(save_to_path))
    if not os.path.exists(save_dir):
        os.makedirs(save_dir)
    defn.copyToHDAFile(save_to_path, new_full_name, menu_name)
    try:
        os.chmod(save_to_path, 0o664)
    except OSError:
        pass
    hou.hda.installFile(save_to_path, force_use_assets=True)

    new_defn = hou.nodeType(node.type().category(), new_full_name).definition()
    new_defn.updateFromNode(node)

    return new_full_name


class TemplateManagerUI(QtWidgets.QWidget):
    def __init__(self):
        super(TemplateManagerUI, self).__init__()
        self.setParent(hou.qt.mainWindow(), QtCore.Qt.Tool)
        self.setWindowTitle("Template Manager")
        self.setMinimumWidth(480)

        self._active_node = None
        self._active_base_name = None
        self._template_data = {}
        self._staging_template_data = {}

        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(16, 16, 16, 16)

        top_form = QtWidgets.QFormLayout()
        top_form.setSpacing(8)

        self._template_combo = QtWidgets.QComboBox()
        self._template_combo.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed
        )
        self._template_combo.currentIndexChanged.connect(self._on_template_changed)
        top_form.addRow("Template:", self._template_combo)

        self._version_combo = QtWidgets.QComboBox()
        self._version_combo.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed
        )
        top_form.addRow("Version:", self._version_combo)

        layout.addLayout(top_form)

        start_row = QtWidgets.QHBoxLayout()
        start_btn = QtWidgets.QPushButton("Start editing")
        start_btn.clicked.connect(self._start_editing)
        cancel_btn = QtWidgets.QPushButton("Cancel Editing")
        cancel_btn.clicked.connect(self._cancel_editing)
        start_row.addWidget(start_btn)
        start_row.addWidget(cancel_btn)
        layout.addLayout(start_row)

        version_row = QtWidgets.QHBoxLayout()
        major_btn = QtWidgets.QPushButton("Increase Major Version")
        major_btn.clicked.connect(lambda: self._increase_version("major"))
        minor_btn = QtWidgets.QPushButton("Increase Minor Version")
        minor_btn.clicked.connect(lambda: self._increase_version("minor"))
        version_row.addWidget(major_btn)
        version_row.addWidget(minor_btn)
        layout.addLayout(version_row)

        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.HLine)
        line.setFrameShadow(QtWidgets.QFrame.Sunken)
        layout.addWidget(line)

        staging_form = QtWidgets.QFormLayout()
        staging_form.setSpacing(8)

        self._staging_template_combo = QtWidgets.QComboBox()
        self._staging_template_combo.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed
        )
        staging_form.addRow("Staging Template:", self._staging_template_combo)

        layout.addLayout(staging_form)

        staging_row = QtWidgets.QHBoxLayout()
        load_btn = QtWidgets.QPushButton("Load Staging Template")
        load_btn.clicked.connect(self._load_staging_template)
        publish_btn = QtWidgets.QPushButton("Publish")
        publish_btn.clicked.connect(self._publish)
        staging_row.addWidget(load_btn)
        staging_row.addWidget(publish_btn)
        layout.addLayout(staging_row)

        layout.setSizeConstraint(QtWidgets.QLayout.SetFixedSize)

        self._populate_templates()
        self._populate_staging_templates()

    def _populate_templates(self):
        self._template_combo.blockSignals(True)
        self._template_combo.clear()
        self._template_data = _get_template_data(_TEMPLATE_PATH, needs_install=False)

        if not self._template_data:
            self._template_combo.addItem("No ALA templates found")
            self._template_combo.setEnabled(False)
            self._version_combo.setEnabled(False)
        else:
            self._template_combo.setEnabled(True)
            self._version_combo.setEnabled(True)
            for name in self._template_data:
                self._template_combo.addItem(name)

        self._template_combo.blockSignals(False)
        self._on_template_changed()

    def _on_template_changed(self):
        self._version_combo.blockSignals(True)
        self._version_combo.clear()
        name = self._template_combo.currentText()
        info = self._template_data.get(name, {})
        for i, v in enumerate(info.get("versions", [])):
            if v == "default":
                label = "Unknown version"
            elif i == 0:
                label = "{} (latest)".format(v)
            else:
                label = v
            self._version_combo.addItem(label, v)
        self._version_combo.blockSignals(False)

    def _populate_staging_templates(self):
        self._staging_template_combo.blockSignals(True)
        self._staging_template_combo.clear()
        self._staging_template_data = _get_template_data(_TEMPLATE_STAGING_PATH, needs_install=True)

        if not self._staging_template_data:
            self._staging_template_combo.addItem("No staging templates found")
            self._staging_template_combo.setEnabled(False)
        else:
            self._staging_template_combo.setEnabled(True)
            for name in self._staging_template_data:
                self._staging_template_combo.addItem(name)

        self._staging_template_combo.blockSignals(False)

    def _get_selected_staging_defn(self):
        base_name = self._staging_template_combo.currentText()
        info = self._staging_template_data.get(base_name)
        if not info or not info["versions"]:
            return None
        version = info["versions"][0]
        return _resolve_defn(info, base_name, version)

    def _get_staging_path(self):
        if self._active_node is None or not _TEMPLATE_STAGING_PATH:
            return ""
        defn = self._active_node.type().definition()
        if defn is None:
            return ""
        lib_filename = os.path.basename(defn.libraryFilePath() or "")
        return os.path.join(_TEMPLATE_STAGING_PATH, lib_filename) if lib_filename else ""

    def _destroy_active_node(self):
        if self._active_node is None:
            return
        try:
            self._active_node.destroy()
        except hou.ObjectWasDeleted:
            pass
        self._active_node = None
        if self._active_base_name:
            _clear_lock(self._active_base_name)
            self._active_base_name = None

    def _create_editing_node(self, full_type_name, base_name=None):
        if base_name:
            lock_info = _read_lock(base_name)
            if lock_info and lock_info["user"] != _current_user():
                if _lock_is_stale(lock_info):
                    confirm = hou.ui.displayMessage(
                        "This template was locked by '{}' but the lock looks stale.\nOverride and edit anyway?".format(lock_info["user"]),
                        buttons=("Override", "Cancel"),
                        severity=hou.severityType.Warning,
                        default_choice=1,
                        close_choice=1,
                    )
                    if confirm != 0:
                        return
                    _clear_lock(base_name)
                else:
                    hou.ui.displayMessage(
                        "This template is currently being edited by '{}'.\nPlease try again later.".format(lock_info["user"]),
                        severity=hou.severityType.Warning,
                    )
                    return

        self._destroy_active_node()

        obj = hou.node("/obj")
        try:
            node = obj.createNode(full_type_name, run_init_scripts=False)
        except hou.OperationFailed as e:
            hou.ui.displayMessage(
                "Failed to create node '{}':\n{}".format(full_type_name, e),
                severity=hou.severityType.Error,
                details=traceback.format_exc(),
                details_label="Traceback",
            )
            return

        self._active_node = node
        if base_name:
            if _write_lock(base_name):
                self._active_base_name = base_name
            else:
                hou.ui.displayMessage(
                    "Someone else just started editing this template, the lock could not be acquired.",
                    severity=hou.severityType.Warning,
                )

        node.allowEditingOfContents()
        hou.clearAllSelected()
        node.setSelected(True)

        pane = hou.ui.paneTabOfType(hou.paneTabType.NetworkEditor)
        if pane is not None:
            pane.setCurrentNode(node)
            pane.homeToSelection()

    def _start_editing(self):
        if not self._template_combo.isEnabled():
            hou.ui.displayMessage(
                "No ALA templates are currently loaded.",
                severity=hou.severityType.Warning,
            )
            return

        base_name = self._template_combo.currentText()
        version = self._version_combo.currentData()

        if base_name in _get_template_data(_TEMPLATE_STAGING_PATH, needs_install=False):
            hou.ui.displayMessage(
                "'{}' already has an unpublished version in staging.\nUse 'Load Staging Template' to review/continue it instead of starting a new edit.".format(base_name),
                severity=hou.severityType.Warning,
            )
            return

        self._create_editing_node(_build_type_name(base_name, version), base_name=base_name)

    def _cancel_editing(self):
        if self._active_node is None:
            hou.ui.displayMessage(
                "No template node is currently being edited.",
                severity=hou.severityType.Warning,
            )
            return
        self._destroy_active_node()

    def _increase_version(self, version_type):
        if self._active_node is None:
            hou.ui.displayMessage(
                "No template node has been created yet. Use 'Start editing' first.",
                severity=hou.severityType.Warning,
            )
            return

        try:
            _ = self._active_node.path()
        except hou.ObjectWasDeleted:
            hou.ui.displayMessage(
                "The template node no longer exists. Use 'Start editing' to create a new one.",
                severity=hou.severityType.Warning,
            )
            self._active_node = None
            return

        save_to = self._get_staging_path()
        if not save_to:
            hou.ui.displayMessage(
                "Could not determine a staging path to save to.",
                severity=hou.severityType.Warning,
            )
            return

        confirm = hou.ui.displayMessage(
            "Increase {} version of '{}'?\nThis will save to:\n{}".format(
                version_type, self._active_node.type().name(), save_to
            ),
            buttons=("Increase Version", "Cancel"),
            severity=hou.severityType.ImportantMessage,
            default_choice=1,
            close_choice=1,
        )
        if confirm != 0:
            return

        try:
            new_name = _increase_hda_version(self._active_node, version_type, save_to)
        except Exception as e:
            hou.ui.displayMessage(
                "Version bump failed:\n{}".format(e),
                severity=hou.severityType.Error,
                details=traceback.format_exc(),
                details_label="Traceback",
            )
            return

        # Release lock so someone else can load the staging template to review it
        if self._active_base_name:
            _clear_lock(self._active_base_name)
            self._active_base_name = None

        self._populate_staging_templates()

        hou.ui.displayMessage(
            "Saved '{}'\nto: {}".format(new_name, save_to)
        )

    def _load_staging_template(self):
        defn = self._get_selected_staging_defn()
        if defn is None:
            hou.ui.displayMessage(
                "No staging template selected.",
                severity=hou.severityType.Warning,
            )
            return

        base_name = self._staging_template_combo.currentText()
        self._create_editing_node(defn.nodeTypeName(), base_name=base_name)

    def _publish(self):
        if not _TEMPLATE_PATH:
            hou.ui.displayMessage(
                "Official templates library path is not set.",
                severity=hou.severityType.Error,
            )
            return

        defn = self._get_selected_staging_defn()
        if defn is None:
            hou.ui.displayMessage(
                "No staging template selected.",
                severity=hou.severityType.Warning,
            )
            return

        filename = os.path.basename(defn.libraryFilePath() or "")
        if not filename:
            hou.ui.displayMessage(
                "Cannot determine the library filename for the selected template.",
                severity=hou.severityType.Warning,
            )
            return

        staging_path  = os.path.join(_TEMPLATE_STAGING_PATH, filename)
        official_path = os.path.join(_TEMPLATE_PATH, filename)

        if not os.path.exists(staging_path):
            hou.ui.displayMessage(
                "No staging file found at:\n{}".format(staging_path),
                severity=hou.severityType.Warning,
            )
            return

        if not os.path.isdir(_TEMPLATE_PATH):
            hou.ui.displayMessage(
                "Official templates library path does not exist:\n{}".format(_TEMPLATE_PATH),
                severity=hou.severityType.Error,
            )
            return

        # Confirm publish
        confirm = hou.ui.displayMessage(
            "Publish '{}' to the official template library?".format(filename),
            buttons=("Publish", "Cancel"),
            severity=hou.severityType.ImportantMessage,
            default_choice=1,
            close_choice=1,
        )
        if confirm != 0:
            return

        # Sync the edits to the staging file, won't bump version 
        if self._active_node is not None:
            try:
                active_defn = self._active_node.type().definition()
            except hou.ObjectWasDeleted:
                active_defn = None
            if (active_defn is not None
                    and active_defn.nodeTypeName() == defn.nodeTypeName()
                    and active_defn.libraryFilePath() == staging_path):
                active_defn.updateFromNode(self._active_node)
                active_defn.copyToHDAFile(staging_path, active_defn.nodeTypeName(), active_defn.description())

        try:
            staging_defs = hou.hda.definitionsInFile(staging_path)
        except Exception as e:
            hou.ui.displayMessage(
                "Failed to read staging file:\n{}".format(e),
                severity=hou.severityType.Error,
                details=traceback.format_exc(),
                details_label="Traceback",
            )
            return

        if not staging_defs:
            hou.ui.displayMessage(
                "No definitions found in staging file.",
                severity=hou.severityType.Warning,
            )
            return

        # Move the staging file to the official path
        try:
            for d in staging_defs:
                d.copyToHDAFile(official_path, d.nodeTypeName(), d.description())
        except Exception as e:
            hou.ui.displayMessage(
                "Failed to copy definitions to official templates library:\n{}".format(e),
                severity=hou.severityType.Error,
                details=traceback.format_exc(),
                details_label="Traceback",
            )
            return

        try:
            os.chmod(official_path, 0o664)
        except OSError:
            pass

        # Remove the staging file
        try:
            os.remove(staging_path)
        except Exception as e:
            hou.ui.displayMessage(
                "Published but could not remove staging file:\n{}".format(e),
                severity=hou.severityType.Warning,
                details=traceback.format_exc(),
                details_label="Traceback",
            )
            return

        # Uninstall the staging file
        try:
            hou.hda.uninstallFile(staging_path)
        except Exception:
            pass

        published_base_name = self._staging_template_combo.currentText()
        _clear_lock(published_base_name)
        if self._active_base_name == published_base_name:
            self._active_base_name = None

        self._populate_templates()
        self._populate_staging_templates()
        hou.ui.displayMessage("Published to:\n{}".format(official_path))


_ui_instance = None

def show_dialog():
    global _ui_instance
    if _ui_instance is not None:
        try:
            _ui_instance.show()
            _ui_instance.raise_()
            _ui_instance.activateWindow()
            return
        except RuntimeError:
            _ui_instance = None

    _ui_instance = TemplateManagerUI()
    main_window = hou.qt.mainWindow()
    if main_window is not None:
        geo = _ui_instance.frameGeometry()
        geo.moveCenter(main_window.frameGeometry().center())
        _ui_instance.move(geo.topLeft())
    _ui_instance.show()