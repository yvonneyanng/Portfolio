from pathlib import Path
import datetime

from Katana import NodegraphAPI, KatanaFile
from UI4.App import Tabs

try:
    from PySide6 import QtCore
    from PySide6 import QtWidgets
except ImportError:
    from PySide2 import QtCore
    from PySide2 import QtWidgets

from pipeline_utils.netcopy_abstract import NetCopy

class KatanaNetCopy(NetCopy):
    def export_selection(self, export_name: str, storage_path: Path) -> bool:
        selected = NodegraphAPI.GetAllSelectedNodes()
        print(f"Selected nodes: {[n.getName() for n in selected]}")
        print(f"Data type: {[type(n) for n in selected]}")

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
        
        scene_name = NodegraphAPI.GetProjectFile()
        scene_base = Path(scene_name).stem if scene_name else "untitled"
        export_path = storage_path / f"{scene_base}_{export_name}.katana"

        if export_path.exists():
            QtWidgets.QMessageBox.warning(
                None,
                "Export Failed",
                f"Export path already exists: {export_path}"
            )
            return False

        try:
            KatanaFile.Export(str(export_path), selected)
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
            # Unselect all nodes first
            selected = list(NodegraphAPI.GetAllSelectedNodes())
            for node in selected:
                    NodegraphAPI.SetNodeSelected(node, False)
            
            KatanaFile.Import(str(import_path))

            node_graph_tab = Tabs.FindTopTab('Node Graph')
            if node_graph_tab:
                    node_graph_tab.frameSelection()

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
        file_exts = ["*.katana"]

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