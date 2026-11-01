from pathlib import Path
import datetime

try:
    from PySide6 import QtWidgets
except ImportError:
    from PySide2 import QtWidgets

import nuke

from pipeline_utils.netcopy_abstract import NetCopy

class NukeNetCopy(NetCopy):
    def export_selection(self, export_name: str, storage_path: Path) -> bool:
        selected = nuke.selectedNodes()
        print(f"Selected nodes: {[n for n in selected]}")

        if not selected:
            QtWidgets.QMessageBox.warning(
                None,
                "Export Failed",
                "No node is selected for export."
            )
            return False
        elif not export_name:
            QtWidgets.QMessageBox.warning(
                None,
                "Export Failed",
                "Please provide a name for the export."
            )
            return False
        
        try:
            scene_name = nuke.scriptName()
        except Exception as e:
            QtWidgets.QMessageBox.warning(
                None,
                "Export Failed",
                f"Failed to export selection.\n\n{e}"
            )
            return False

        scene_base = Path(scene_name).stem
        export_path = storage_path / f"{scene_base}_{export_name}.nk"

        if export_path.exists():
            QtWidgets.QMessageBox.warning(
                None,
                "Export Failed",
                f"Export path already exists: {export_path}"
            )
            return False

        try:
            nuke.nodeCopy(str(export_path))
            QtWidgets.QMessageBox.information(
                None,
                "Export Succeeded",
                f"Exported selection to: {export_path}"
            )
            return True
        except Exception as e:
            QtWidgets.QMessageBox.warning(
                None,
                "Export Failed",
                f"Failed to export selection.\n\n{e}"
            )
            return False

    def import_copy(self, import_path: Path) -> bool:
        if not import_path.exists():
            QtWidgets.QMessageBox.warning(
                None,
                "Import Failed",
                f"Import path does not exist: {import_path}"
            )
            return False

        try:
            nuke.nodePaste(str(import_path))

            print(f"Imported selection from: {import_path}")
            return True
        except Exception as e:
            QtWidgets.QMessageBox.warning(
                None,
                "Import Failed",
                f"Failed to import file. Make sure the file is valid.\n\n{e}"
            )

            return False

    def get_all_copies(self, storage_path: Path) -> dict[Path, str]:
        """Public interface: Retrieve all available copies with their timestamps."""
        file_exts = ["*.nk"]

        copies = []
        for ext in file_exts:
            copies.extend(storage_path.glob(ext))

        copies_and_times = {}

        for copy in copies:
            timestamp = copy.stat().st_mtime
            copies_and_times[copy] = timestamp

        copies_and_times = dict(sorted(copies_and_times.items(), key=lambda item: item[1], reverse=True))

        for copy, date in copies_and_times.items():
            m_date_time = datetime.datetime.fromtimestamp(date)
            formatted_time = m_date_time.strftime("%Y-%m-%d %H:%M:%S")
            copies_and_times[copy] = formatted_time

        return copies_and_times

    def get_main_window(self) -> QtWidgets.QWidget:
        """Public interface: Get the main window instance."""
        app = QtWidgets.QApplication.instance()
        if app:
            # Return the active window or the main window
            main_window = app.activeWindow()
            if main_window:
                return main_window
            # Fallback: return the first top-level widget
            for widget in app.topLevelWidgets():
                if isinstance(widget, QtWidgets.QMainWindow):
                    return widget
        return None