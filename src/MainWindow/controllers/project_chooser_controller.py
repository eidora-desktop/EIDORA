from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Callable

from PySide6 import QtCore, QtGui, QtWidgets

from MainWindow.controllers.project_chooser_row import (
    PROJECT_ROW_HEIGHT,
    ProjectRowWidget,
)
from project.service import ProjectService
from shared.qt.branding import branding_pixmap, branding_svg_pixmap
from shared.qt.message_dialogs import show_error
from shared.qt.ui_loader import load_ui
from shared.qt.theme import THEMES

if TYPE_CHECKING:
    from project.domain import Project


_PROJECT_NAME_ROLE = QtCore.Qt.ItemDataRole.UserRole + 1


class _ProjectItemDelegate(QtWidgets.QStyledItemDelegate):
    """Paint project names and paths using the same primary text color."""

    _ROW_HEIGHT = PROJECT_ROW_HEIGHT
    _HORIZONTAL_PADDING = 12
    _LINE_GAP = 3

    def paint(
        self,
        painter: QtGui.QPainter,
        option: QtWidgets.QStyleOptionViewItem,
        index: QtCore.QModelIndex,
    ) -> None:
        styled_option = QtWidgets.QStyleOptionViewItem(option)
        self.initStyleOption(styled_option, index)
        styled_option.text = ""
        style = (
            styled_option.widget.style()
            if styled_option.widget is not None
            else QtWidgets.QApplication.style()
        )
        style.drawControl(
            QtWidgets.QStyle.ControlElement.CE_ItemViewItem,
            styled_option,
            painter,
            styled_option.widget,
        )
        if (
            isinstance(styled_option.widget, QtWidgets.QAbstractItemView)
            and styled_option.widget.indexWidget(index) is not None
        ):
            return

        name = str(index.data(_PROJECT_NAME_ROLE) or "")
        path = str(index.data(QtCore.Qt.ItemDataRole.UserRole) or "")
        content = option.rect.adjusted(
            self._HORIZONTAL_PADDING,
            0,
            -self._HORIZONTAL_PADDING,
            0,
        )
        title_font = self._title_font(styled_option.font)
        title_metrics = QtGui.QFontMetrics(title_font)
        path_metrics = styled_option.fontMetrics
        content_height = title_metrics.height() + self._LINE_GAP + path_metrics.height()
        top = content.center().y() - content_height // 2

        selected = bool(
            styled_option.state & QtWidgets.QStyle.StateFlag.State_Selected
        )
        title_role = (
            QtGui.QPalette.ColorRole.HighlightedText
            if selected
            else QtGui.QPalette.ColorRole.Text
        )

        painter.save()
        painter.setFont(title_font)
        painter.setPen(styled_option.palette.color(title_role))
        painter.drawText(
            content.left(),
            top + title_metrics.ascent(),
            title_metrics.elidedText(
                name,
                QtCore.Qt.TextElideMode.ElideRight,
                content.width(),
            ),
        )
        painter.setFont(styled_option.font)
        painter.setPen(styled_option.palette.color(title_role))
        painter.drawText(
            content.left(),
            top + title_metrics.height() + self._LINE_GAP + path_metrics.ascent(),
            path_metrics.elidedText(
                path,
                QtCore.Qt.TextElideMode.ElideMiddle,
                content.width(),
            ),
        )
        painter.restore()

    def sizeHint(
        self,
        option: QtWidgets.QStyleOptionViewItem,
        index: QtCore.QModelIndex,
    ) -> QtCore.QSize:
        size = super().sizeHint(option, index)
        size.setWidth(0)
        size.setHeight(max(size.height(), self._ROW_HEIGHT))
        return size

    @staticmethod
    def _title_font(base_font: QtGui.QFont) -> QtGui.QFont:
        font = QtGui.QFont(base_font)
        if font.pointSizeF() > 0:
            font.setPointSizeF(font.pointSizeF() + 2.0)
        elif font.pixelSize() > 0:
            font.setPixelSize(font.pixelSize() + 3)
        font.setWeight(QtGui.QFont.Weight.Medium)
        return font


class _ChooserPaletteFilter(QtCore.QObject):
    """Keep the wordmark legible when the application palette changes."""

    def __init__(self, dialog: QtWidgets.QDialog, refresh) -> None:
        super().__init__(dialog)
        self._dialog = dialog
        self._refresh = refresh
        dialog.installEventFilter(self)

    def eventFilter(self, watched: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if watched is self._dialog and event.type() == QtCore.QEvent.Type.PaletteChange:
            self._refresh()
        return False


class ProjectChooserController:
    """Standalone launcher for selecting or adding a project."""

    def __init__(
        self,
        project_service: ProjectService,
        *,
        remove_project_callback: Callable[[Project], bool] | None = None,
        storage_read_only: bool = False,
    ) -> None:
        self.dialog = load_ui("MainWindow", "ui/project_chooser.ui", None)
        if not isinstance(self.dialog, QtWidgets.QDialog):
            raise RuntimeError("project_chooser.ui root must be a QDialog")
        self.dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        self._project_service = project_service
        self._remove_project_callback = remove_project_callback
        self._storage_read_only = storage_read_only
        self._connections = []
        self._selected_project: Project | None = None
        self._selection_status = ""
        self._projects_by_path: dict[Path, Project] = {}
        self._label_logo = self._find_required(QtWidgets.QLabel, "label_logo")
        self._label_wordmark = self._find_required(QtWidgets.QLabel, "label_wordmark")

        self._list_projects = self._find_required(QtWidgets.QListWidget, "list_projects")
        self._project_item_delegate = _ProjectItemDelegate(self._list_projects)
        self._list_projects.setItemDelegate(self._project_item_delegate)
        self._label_no_projects = self._find_required(QtWidgets.QLabel, "label_no_projects")
        self._button_open_project = self._find_required(
            QtWidgets.QPushButton, "button_open_project"
        )
        self._button_new_project = self._find_required(
            QtWidgets.QPushButton, "button_new_project"
        )
        self._button_import_project = self._find_required(
            QtWidgets.QPushButton, "button_import_project"
        )
        self._button_storage = self._find_required(QtWidgets.QPushButton, "button_storage")
        self._button_starter = self._find_required(QtWidgets.QPushButton, "button_starter")
        from project.starter_collection import bundled_resources, read_manifest

        manifest_path = bundled_resources()
        try:
            manifest = read_manifest(manifest_path)
            starter_available = (manifest_path.parent / manifest["archive"]).is_file()
        except (OSError, ValueError):
            starter_available = False
        self._button_starter.setVisible(starter_available)
        self._refresh_branding()
        self._palette_filter = _ChooserPaletteFilter(self.dialog, self._refresh_branding)

        self._wire()
        self.refresh()

    @property
    def selected_project(self) -> Project | None:
        return self._selected_project

    @property
    def selection_status(self) -> str:
        return self._selection_status

    def exec(self) -> QtWidgets.QDialog.DialogCode:
        self.refresh()
        if self._list_projects.count():
            self._list_projects.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)
        else:
            self._button_new_project.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)
        return QtWidgets.QDialog.DialogCode(self.dialog.exec())

    def refresh(self) -> None:
        selected_path = self._selected_project_path()
        recent_projects = self._project_service.list_recent_project_entries()
        self._list_projects.clear()
        self._projects_by_path = {
            entry.project.root: entry.project for entry in recent_projects
        }

        selected_row = 0
        for row, entry in enumerate(recent_projects):
            project = entry.project
            item = QtWidgets.QListWidgetItem(f"{project.name}\n{project.root}")
            item.setData(QtCore.Qt.ItemDataRole.UserRole, str(project.root))
            item.setData(_PROJECT_NAME_ROLE, project.name)
            item.setToolTip(str(project.root))
            self._list_projects.addItem(item)
            self._list_projects.setItemWidget(
                item,
                ProjectRowWidget(
                    project,
                    select_callback=self._select_project_path,
                    open_callback=self._open_selected_project,
                    rename_callback=self._rename_project,
                    remove_callback=self._remove_project,
                    parent=self._list_projects,
                ),
            )
            if selected_path is not None and project.root == selected_path:
                selected_row = row

        has_projects = bool(recent_projects)
        self._list_projects.setVisible(has_projects)
        self._label_no_projects.setVisible(not has_projects)
        self._button_new_project.setDefault(not has_projects)
        self._button_open_project.setDefault(has_projects)
        if has_projects:
            self._list_projects.setCurrentItem(
                self._list_projects.item(selected_row),
                QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect,
            )
        self._update_open_button()

    def dispose(self) -> None:
        self.dialog.removeEventFilter(self._palette_filter)
        self._palette_filter = None
        for signal, slot in self._connections:
            signal.disconnect(slot)
        self._connections.clear()

    def _refresh_branding(self) -> None:
        dark = self.dialog.palette().color(QtGui.QPalette.ColorRole.Window).lightness() < 128
        ratio = self.dialog.devicePixelRatioF()
        self._label_logo.setPixmap(
            branding_pixmap(
                "eidora_logo_ui.png",
                QtCore.QSize(68, 68),
                device_pixel_ratio=ratio,
            )
        )
        self._label_wordmark.setPixmap(
            branding_svg_pixmap(
                "eidora_wordmark.svg",
                QtCore.QSize(144, 27),
                device_pixel_ratio=ratio,
                ink=THEMES["dark" if dark else "light"].text,
            )
        )

    def _wire(self) -> None:
        self._connections = [
            (self._button_new_project.clicked, self._create_project),
            (self._button_import_project.clicked, self._import_existing_project),
            (self._button_open_project.clicked, self._open_selected_project),
            (self._list_projects.itemSelectionChanged, self._update_open_button),
            (self._list_projects.itemDoubleClicked, lambda _item: self._open_selected_project()),
            (self._button_storage.clicked, self._open_storage),
            (self._button_starter.clicked, self._create_starter),
        ]
        for signal, slot in self._connections:
            signal.connect(slot)

    def _create_project(self) -> None:
        from MainWindow.controllers.new_project_dialog_controller import choose_new_project

        selection = choose_new_project(self._project_service)
        if selection is None:
            return
        project_name, parent = selection
        try:
            project = self._project_service.create_empty_project(project_name, parent=parent)
        except Exception as exc:
            show_error(self.dialog, "New Project Failed", str(exc))
            self.refresh()
            return
        self._accept_project(project, f"Created project {project.name}")

    def _open_storage(self) -> None:
        from MainWindow.controllers.storage_dialog_controller import StorageDialogController

        controller = StorageDialogController(self._project_service, read_only=self._storage_read_only)
        controller.exec()
        self.refresh()

    def _create_starter(self) -> None:
        from MainWindow.controllers.starter_collection_controller import prepare_bundled_starter
        from project.starter_collection import bundled_resources, read_manifest
        from MainWindow.controllers.storage_dialog_controller import format_bytes

        manifest = read_manifest(bundled_resources())
        answer = QtWidgets.QMessageBox.question(
            self.dialog, "Create MET Example Project",
            f"Create a new MET example project ({format_bytes(manifest['extracted_bytes'])}) inside:\n"
            f"{self._project_service.workspace_root}\n\n"
            "Existing projects are preserved. This project remains after uninstalling EIDORA.",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.Yes,
        )
        if answer == QtWidgets.QMessageBox.StandardButton.Yes:
            prepare_bundled_starter(self._project_service, recreate=True)
            self.refresh()

    def _import_existing_project(self) -> None:
        project_path = QtWidgets.QFileDialog.getExistingDirectory(
            self.dialog,
            "Import Existing Project",
        )
        if not project_path:
            return
        try:
            project = self._project_service.open_project(project_path)
        except Exception as exc:
            show_error(self.dialog, "Import Existing Project Failed", str(exc))
            self.refresh()
            return
        self._accept_project(project, f"Imported existing project {project.name}")

    def _open_selected_project(self) -> None:
        project_path = self._selected_project_path()
        if project_path is None:
            return
        try:
            project = self._project_service.open_recent_project(project_path)
        except Exception as exc:
            show_error(self.dialog, "Open Project Failed", str(exc))
            self.refresh()
            return
        self._accept_project(project, f"Opened project {project.name}")

    def _rename_project(self, project_path: Path) -> None:
        project = self._projects_by_path.get(project_path)
        if project is None:
            self.refresh()
            return
        project_name, accepted = QtWidgets.QInputDialog.getText(
            self.dialog,
            "Rename Project",
            "Project name:",
            text=project.name,
        )
        if not accepted:
            return
        try:
            self._project_service.rename_project(project, project_name)
        except Exception as exc:
            show_error(self.dialog, "Rename Project Failed", str(exc))
        self.refresh()

    def _remove_project(self, project_path: Path) -> None:
        project = self._projects_by_path.get(project_path)
        if project is None:
            self.refresh()
            return
        if not self._confirm_remove_project(project):
            return
        if self._remove_project_callback is not None:
            if self._remove_project_callback(project):
                self.refresh()
            return
        try:
            self._project_service.delete_project(project)
        except Exception as exc:
            show_error(self.dialog, "Remove Project Failed", str(exc))
        self.refresh()

    def _confirm_remove_project(self, project: Project) -> bool:
        message = (
            f'This will permanently delete project "{project.name}" and all data '
            "stored in its folder.\n\n"
            f"{project.root}\n\n"
            "This cannot be undone."
        )
        answer = QtWidgets.QMessageBox.warning(
            self.dialog,
            "Remove Project",
            message,
            QtWidgets.QMessageBox.StandardButton.Yes
            | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        return answer == QtWidgets.QMessageBox.StandardButton.Yes

    def _select_project_path(self, project_path: Path) -> None:
        for row in range(self._list_projects.count()):
            item = self._list_projects.item(row)
            if Path(str(item.data(QtCore.Qt.ItemDataRole.UserRole))) == project_path:
                self._list_projects.setCurrentItem(
                    item,
                    QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect,
                )
                return

    def _accept_project(self, project: Project, status: str) -> None:
        self._selected_project = project
        self._selection_status = status
        self.dialog.accept()

    def _selected_project_path(self) -> Path | None:
        item = self._list_projects.currentItem()
        if item is None:
            return None
        value = item.data(QtCore.Qt.ItemDataRole.UserRole)
        return Path(str(value)) if value else None

    def _update_open_button(self) -> None:
        self._button_open_project.setEnabled(self._selected_project_path() is not None)

    def _find_required(self, widget_type: type, object_name: str):
        widget = self.dialog.findChild(widget_type, object_name)
        if widget is None:
            raise RuntimeError(f"project_chooser.ui is missing {object_name}")
        return widget
