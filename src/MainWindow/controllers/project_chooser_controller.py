from __future__ import annotations

from collections.abc import Iterator
import errno
from pathlib import Path
import sqlite3
from typing import TYPE_CHECKING, Callable

from PySide6 import QtCore, QtGui, QtWidgets
from shiboken6 import isValid

from MainWindow.controllers.loading_panel_controller import LoadingPanelController
from MainWindow.controllers.project_chooser_row import (
    PROJECT_ROW_HEIGHT,
    ProjectRowWidget,
)
from project.service import ProjectService
from shared.qt.item_views import clear_list_items
from shared.qt.branding import branding_pixmap, branding_svg_pixmap
from shared.qt.message_dialogs import show_error
from shared.qt.ui_loader import load_ui
from shared.qt.theme import THEMES
from MainWindow.controllers.diagnostics_dialog_controller import show_diagnostics
from shared.qt.diagnostics import report_repository
from shared.diagnostics.service import LOGGER

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


class _ChooserMenuFilter(QtCore.QObject):
    """Position mouse-opened menus at the click; retain native keyboard behavior."""

    def __init__(self, button: QtWidgets.QPushButton) -> None:
        super().__init__(button)
        self._button = button
        self._menu = button.menu()
        self._menu.aboutToHide.connect(self._reset_button)
        button.installEventFilter(self)

    def eventFilter(self, watched: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if watched is self._button and event.type() in (
            QtCore.QEvent.Type.MouseButtonPress,
            QtCore.QEvent.Type.MouseButtonRelease,
        ) and event.button() == QtCore.Qt.MouseButton.LeftButton:
            if event.type() == QtCore.QEvent.Type.MouseButtonPress:
                self._button.setFocus(QtCore.Qt.FocusReason.MouseFocusReason)
                self._button.setDown(True)
                self._menu.popup(event.globalPosition().toPoint())
            return True
        return False

    @QtCore.Slot()
    def _reset_button(self) -> None:
        if isValid(self._button):
            self._button.setDown(False)


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
        prepare_project: Callable[[Project], Iterator[None] | None] | None = None,
        open_settings: Callable[[str], None] | None = None,
    ) -> None:
        self.dialog = load_ui("MainWindow", "ui/project_chooser.ui", None)
        self._settings_callback = open_settings
        if not isinstance(self.dialog, QtWidgets.QDialog):
            raise RuntimeError("project_chooser.ui root must be a QDialog")
        self.dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        self._project_service = project_service
        self._remove_project_callback = remove_project_callback
        self._storage_read_only = storage_read_only
        self._prepare_project = prepare_project
        self._opening_project = False
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
        self._open_project_menu = QtWidgets.QMenu(self._button_import_project)
        self._action_open_folder = self._open_project_menu.addAction("Folder…")
        self._action_import_bundle = self._open_project_menu.addAction("ZIP…")
        self._button_import_project.setMenu(self._open_project_menu)
        button_more_options = self._find_required(QtWidgets.QPushButton, "button_more_options")
        self._more_options_menu = QtWidgets.QMenu(button_more_options)
        self._action_settings = self._more_options_menu.addAction("Settings...")
        self._more_options_menu.addSeparator()
        self._action_starter = self._more_options_menu.addAction("Create MET Example…")
        self._action_report = self._more_options_menu.addAction("Diagnostics…")
        button_more_options.setMenu(self._more_options_menu)
        self._menu_filters = [
            _ChooserMenuFilter(button)
            for button in (self._button_import_project, button_more_options)
        ]
        self._diagnostics_notice = self._find_required(QtWidgets.QLabel, "label_diagnostics_notice")
        self._diagnostics_notice.setVisible(report_repository().has_unreviewed_problem())
        from project.starter_collection import bundled_resources, read_manifest

        manifest_path = bundled_resources()
        try:
            manifest = read_manifest(manifest_path)
            starter_available = (manifest_path.parent / manifest["archive"]).is_file()
        except (OSError, ValueError):
            starter_available = False
        self._action_starter.setVisible(starter_available)
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
        # QDialog.exec() ends when the chooser is hidden for loading. Wait
        # for accept/reject instead, so an opening failure can restore it.
        loop = QtCore.QEventLoop()
        app = QtWidgets.QApplication.instance()
        self.dialog.setResult(QtWidgets.QDialog.DialogCode.Rejected)
        self.dialog.finished.connect(loop.quit)
        app.aboutToQuit.connect(loop.quit)
        try:
            self.dialog.show()
            loop.exec()
            return QtWidgets.QDialog.DialogCode(self.dialog.result())
        finally:
            self.dialog.finished.disconnect(loop.quit)
            app.aboutToQuit.disconnect(loop.quit)

    def refresh(self) -> None:
        selected_path = self._selected_project_path()
        projects = self._project_service.list_known_projects(recent_first=True)
        clear_list_items(self._list_projects)
        self._projects_by_path = {
            project.root: project for project in projects
        }

        selected_row = 0
        for row, project in enumerate(projects):
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

        has_projects = bool(projects)
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
        if self._palette_filter is None:
            return
        self.dialog.removeEventFilter(self._palette_filter)
        self._palette_filter = None
        for signal, slot in self._connections:
            signal.disconnect(slot)
        self._connections.clear()
        clear_list_items(self._list_projects)
        # Row callbacks also retain this controller. Delete the entire transient
        # tree on the GUI thread rather than relying on collection of Qt cycles.
        self.dialog.deleteLater()

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
            (self._action_open_folder.triggered, self._import_existing_project),
            (self._button_open_project.clicked, self._open_selected_project),
            (self._list_projects.itemSelectionChanged, self._update_open_button),
            (self._list_projects.itemDoubleClicked, lambda _item: self._open_selected_project()),
            (self._action_settings.triggered, self._open_settings),
            (self._action_report.triggered, self._show_report),
            (self._action_starter.triggered, self._create_starter),
            (self._action_import_bundle.triggered, self._import_project_bundle),
        ]
        for signal, slot in self._connections:
            signal.connect(slot)

    def _create_project(self) -> None:
        from MainWindow.controllers.new_project_dialog_controller import choose_new_project

        selection = choose_new_project(self._project_service)
        if selection is None:
            return
        project_name, parent = selection
        self._load_project(
            lambda: self._project_service.create_empty_project(project_name, parent=parent),
            "New Project Failed",
            "Created project",
        )

    def _show_report(self) -> None:
        show_diagnostics()
        self._diagnostics_notice.setVisible(report_repository().has_unreviewed_problem())

    def _open_settings(self) -> None:
        from MainWindow.controllers.settings_dialog_controller import SettingsDialogController

        if self._settings_callback is not None:
            self._settings_callback("storage")
        else:
            controller = SettingsDialogController(self._project_service, project_open=self._storage_read_only)
            controller.select_tab("storage")
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
        from shared.qt import file_dialogs

        project_path = file_dialogs.get_import_directory(
            self.dialog,
            "Open Project Folder",
        )
        if not project_path:
            return
        self._load_project(
            lambda: self._project_service.open_project(project_path),
            "Open Project Folder Failed",
            "Opened project",
            source_root=Path(project_path),
        )

    def _import_project_bundle(self) -> None:
        bundle_path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self.dialog, "Open Project ZIP", "", "Project Bundles (*.zip)",
        )
        if bundle_path:
            self._load_project(
                lambda: self._project_service.import_project_bundle(bundle_path),
                "Open Project ZIP Failed", "Opened project from ZIP",
            )

    def _open_selected_project(self) -> None:
        project_path = self._selected_project_path()
        if project_path is None:
            return
        self._load_project(
            lambda: self._project_service.open_recent_project(project_path),
            "Open Project Failed",
            "Opened project",
            source_root=project_path,
        )

    def _load_project(
        self,
        load: Callable[[], Project],
        error_title: str,
        status_prefix: str,
        *,
        source_root: Path | None = None,
    ) -> None:
        if self._opening_project:
            return
        self._opening_project = True
        copy_requested = False
        LOGGER.info("Opening project")
        try:
            self.dialog.hide()
            loading = LoadingPanelController(owner=self.dialog)
            project = loading.run(load, self._prepare_project)
        except Exception as exc:
            self.dialog.show()
            if source_root is not None and self._is_project_write_failure(exc, source_root):
                LOGGER.warning("Project opening needs write access: %s", source_root, exc_info=True)
                copy_requested = self._offer_project_copy(source_root, exc)
            else:
                show_error(self.dialog, error_title, str(exc))
            self.refresh()
        else:
            LOGGER.info("Project opened")
            self._accept_project(project, f"{status_prefix} {project.name}")
        finally:
            self._opening_project = False
        if copy_requested:
            self._load_project(
                lambda: self._project_service.copy_project_to_workspace(source_root),
                "Copy Project Failed", "Opened project copy",
            )

    @staticmethod
    def _is_project_write_failure(error: Exception, root: Path) -> bool:
        code = getattr(error, "sqlite_errorcode", 0) or 0
        if isinstance(error, sqlite3.Error) and code & 0xff == sqlite3.SQLITE_READONLY:
            return True
        if isinstance(error, OSError) and error.errno in {errno.EACCES, errno.EPERM, errno.EROFS}:
            # Copying the project cannot repair access to the recent-history file.
            return bool(error.filename and Path(error.filename).resolve().is_relative_to(root.resolve()))
        return False

    def _offer_project_copy(self, root: Path, error: Exception) -> bool:
        message = QtWidgets.QMessageBox()
        message.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        message.setIcon(QtWidgets.QMessageBox.Icon.Warning)
        message.setWindowTitle("Project Needs Write Access")
        message.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        location = getattr(error, "filename", None) or str(root)
        message.setText(f"EIDORA could not write to this project location:\n{location}")
        message.setInformativeText(
            "Opening a project requires write access to its databases and their folders. "
            "You can copy it to your project storage and open the copy, leaving the original unchanged. "
            "Close the source project in other EIDORA windows before copying.\n\n"
            f"Copy destination: {self._project_service.workspace_root}"
        )
        message.setDetailedText(str(error))
        copy_button = message.addButton("Copy to My Projects and Open", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        cancel = message.addButton(QtWidgets.QMessageBox.StandardButton.Cancel)
        message.setDefaultButton(cancel)
        message.exec()
        return message.clickedButton() is copy_button

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
