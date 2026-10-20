from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6 import QtCore, QtWidgets

from FiguresArea.controllers.figure_settings_panel_controller import FigureSettingsPanelController
from FiguresArea.controllers.snapshot_sources_panel_controller import SnapshotSourcesPanelController
from FiguresArea.repository import FigureTabsRepository
from DiscoveryArea.presets_repository import PresetRepository
from shared.qt.icons import apply_icon_color
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import add_widget_to_container, connect_placeholder_buttons, require_child

if TYPE_CHECKING:
    from FiguresArea.controllers.plot_tab_controller import PlotTabController
    from FiguresArea.controllers.snapshot_animation_tab_controller import AnimationTabController
    from project.domain import Project
    from shared.qt.activity import ActivityManager
    from shared.qt.panel_controller import UiPanelController


class FiguresAreaController:
    """UI binder for the figures area."""

    def __init__(
        self,
        parent: QtWidgets.QWidget | None = None,
        status_callback: Callable[[str], None] | None = None,
        activity_manager: ActivityManager | None = None,
    ) -> None:
        self._widget = load_ui("FiguresArea", "ui/figures_area.ui", parent)
        self._project: Project | None = None
        self._status_callback = status_callback
        self._activity_manager = activity_manager
        self._panels: list[UiPanelController] = []
        self._figure_settings_panel: FigureSettingsPanelController | None = None
        self._plot_tabs: dict[
            QtWidgets.QWidget, PlotTabController | AnimationTabController
        ] = {}
        self._closed_figure_tabs: list[dict[str, object]] = []
        self._restoring_project_state = False
        self._restored_project_root: Path | None = None
        self._state_save_timer = QtCore.QTimer(self._widget)
        self._state_save_timer.setSingleShot(True)
        self._state_save_timer.setInterval(250)
        self._state_save_timer.timeout.connect(self._save_project_tab_state)
        self._load_panels()
        connect_placeholder_buttons(self._widget, self._placeholder_action)
        self._configure_figure_tabs()
        self._configure_plot_option_buttons()

    @property
    def widget(self) -> QtWidgets.QWidget:
        return self._widget

    def set_project(self, project: Project | None) -> None:
        if self._activity_manager is not None:
            with self._activity_manager.busy("Refreshing figures…"):
                self._set_project(project)
            return
        self._set_project(project)

    def _set_project(self, project: Project | None) -> None:
        incoming_root = project.root if project is not None else None
        if self._project is not None and self._project.root != incoming_root:
            self._save_project_tab_state()
        project_changed = self._project is None or self._project.root != incoming_root
        self._project = project
        if project_changed:
            self._clear_figure_tabs()
            self._restored_project_root = None
        for plot_tab in self._plot_tabs.values():
            plot_tab.set_project(project)
        for panel in self._panels:
            panel.set_project(project)
        self._refresh_figure_settings_preset_menu()
        label = self._widget.findChild(QtWidgets.QLabel, "label_project_status")
        if label is not None:
            label.setText(project.name if project is not None else "No project loaded")

    def shutdown(self) -> None:
        self._save_project_tab_state()
        for plot in self._plot_tabs.values():
            shutdown = getattr(plot, "shutdown", None)
            if callable(shutdown):
                shutdown()
        for panel in self._panels:
            panel.shutdown()

    def activate(self) -> None:
        self._ensure_project_state_loaded()
        for plot in self._plot_tabs.values():
            activate = getattr(plot, "activate", None)
            if callable(activate):
                activate()
        self._sync_settings_panel_with_current_tab()

    def refresh_snapshots(self) -> None:
        activity = (
            self._activity_manager.busy("Refreshing snapshots…")
            if self._activity_manager is not None
            else None
        )
        try:
            for panel in self._panels:
                if isinstance(panel, SnapshotSourcesPanelController):
                    panel.refresh_snapshots(show_activity=False)
        finally:
            if activity is not None:
                activity.finish()

    def _load_panels(self) -> None:
        panel_specs = [
            ("panel_snapshots_host", SnapshotSourcesPanelController),
            ("panel_figure_settings_host", FigureSettingsPanelController),
        ]
        for container_name, controller_type in panel_specs:
            container = require_child(self._widget, QtWidgets.QWidget, container_name)
            panel = controller_type(
                parent=self._widget,
                status_callback=self._show_status,
                activity_manager=self._activity_manager,
            )
            add_widget_to_container(container, panel.widget)
            self._panels.append(panel)
            if isinstance(panel, FigureSettingsPanelController):
                self._figure_settings_panel = panel
                panel.set_save_preset_callback(self._save_figure_settings_preset)
                panel.set_load_preset_callback(self._load_figure_settings_preset_by_label)

    def _placeholder_action(self, label: str) -> None:
        self._show_status(f"Figures action '{label}' is not implemented yet.")

    def _configure_figure_tabs(self) -> None:
        tabs = require_child(self._widget, QtWidgets.QTabWidget, "tabs_figures")
        tabs.setTabsClosable(True)
        tabs.tabCloseRequested.connect(self._close_figure_tab)
        tabs.currentChanged.connect(lambda _index: self._sync_settings_panel_with_current_tab())
        self._hide_add_tab_close_button(tabs)

    def _configure_plot_option_buttons(self) -> None:
        specs = [
            ("button_create_journal_plot", "journal", "journal", "Journal / print"),
            (
                "button_create_presentation_plot",
                "presentation",
                "presentation",
                "Web / presentation",
            ),
            (
                "button_create_animation_plot",
                "animation",
                "animation",
                "Animation",
            ),
        ]
        for button_name, icon_name, figure_kind, title_prefix in specs:
            button = require_child(self._widget, QtWidgets.QToolButton, button_name)
            try:
                button.clicked.disconnect()
            except RuntimeError:
                pass
            button.setProperty("iconName", icon_name)
            button.setIconSize(QtCore.QSize(64, 64))
            button.setStyleSheet("QToolButton { text-align: center; padding: 8px; }")
            button.clicked.connect(
                lambda _checked=False, kind=figure_kind, prefix=title_prefix: self._create_figure_tab(kind, prefix)
            )
        apply_icon_color(self._widget)

    def _create_figure_tab(self, figure_kind: str, title_prefix: str) -> None:
        self._ensure_project_state_loaded()
        tabs = require_child(self._widget, QtWidgets.QTabWidget, "tabs_figures")
        title = f"{title_prefix} {self._next_figure_number(tabs)}"
        self._add_figure_tab(figure_kind, title)
        self._show_status(f"Created {title}.")

    def _add_figure_tab(
        self,
        figure_kind: str,
        title: str,
        settings: object | None = None,
        composition: object | None = None,
    ) -> None:
        from FiguresArea.controllers.plot_tab_controller import PlotTabController
        from FiguresArea.controllers.snapshot_animation_tab_controller import AnimationTabController

        tabs = require_child(self._widget, QtWidgets.QTabWidget, "tabs_figures")
        add_page = require_child(self._widget, QtWidgets.QWidget, "page_add_figure")
        add_index = tabs.indexOf(add_page)
        if add_index < 0:
            add_index = tabs.count()

        normalized_kind = "animation" if figure_kind == "snapshot_animation" else figure_kind
        controller_type = (
            AnimationTabController
            if normalized_kind == "animation"
            else PlotTabController
        )
        controller_kwargs = {
            "project": self._project,
            "project_root": self._project.root if self._project is not None else None,
            "parent": tabs,
            "status_callback": self._show_status,
            "selection_callback": self._show_figure_settings_for_selection,
            "state_changed_callback": self._schedule_project_tab_state_save,
            "activity_manager": self._activity_manager,
        }
        if controller_type is PlotTabController:
            controller_kwargs["figure_kind"] = normalized_kind
        plot = controller_type(**controller_kwargs)
        if settings is not None:
            plot.apply_figure_settings(
                FigureTabsRepository.settings_from_payload(
                    settings, figure_kind=normalized_kind
                )
            )
        if isinstance(plot, PlotTabController):
            restored_composition = FigureTabsRepository.composition_from_payload(
                composition,
                settings=plot.figure_settings(),
            )
            if restored_composition is not None:
                plot.apply_figure_composition(restored_composition)
        page = plot.widget
        page.setObjectName(f"page_figure_{normalized_kind}_{len(self._plot_tabs) + 1}")
        page.setProperty("figureKind", normalized_kind)
        page.setProperty("figureTitle", title)
        self._plot_tabs[page] = plot

        if isinstance(plot, AnimationTabController) and isinstance(settings, dict):
            animation_payload = settings.get("animation", settings.get("snapshot_animation"))
            if isinstance(animation_payload, dict):
                plot.apply_animation_state_payload(animation_payload)

        index = tabs.insertTab(add_index, page, title)
        tabs.setCurrentIndex(index)
        self._hide_add_tab_close_button(tabs)
        self._sync_settings_panel_with_current_tab()
        self._save_project_tab_state()

    def _close_figure_tab(self, index: int) -> None:
        tabs = require_child(self._widget, QtWidgets.QTabWidget, "tabs_figures")
        if index < 0 or index >= tabs.count():
            return
        page = tabs.widget(index)
        if page is None or page.objectName() == "page_add_figure":
            self._hide_add_tab_close_button(tabs)
            return
        record = self._figure_tab_record(tabs, index)
        if record is not None:
            self._closed_figure_tabs.append(record)
        tabs.removeTab(index)
        plot = self._plot_tabs.pop(page, None)
        shutdown = getattr(plot, "shutdown", None)
        if callable(shutdown):
            shutdown()
        page.deleteLater()
        self._hide_add_tab_close_button(tabs)
        self._sync_settings_panel_with_current_tab()
        self._save_project_tab_state()

    def _clear_figure_tabs(self) -> None:
        tabs = require_child(self._widget, QtWidgets.QTabWidget, "tabs_figures")
        for page in tuple(self._plot_tabs):
            plot = self._plot_tabs.get(page)
            shutdown = getattr(plot, "shutdown", None)
            if callable(shutdown):
                shutdown()
            index = tabs.indexOf(page)
            if index >= 0:
                tabs.removeTab(index)
            page.deleteLater()
        self._plot_tabs.clear()
        self._closed_figure_tabs.clear()
        self._hide_add_tab_close_button(tabs)

    def _save_project_tab_state(self) -> None:
        self._state_save_timer.stop()
        if (
            self._project is None
            or self._restoring_project_state
            or self._restored_project_root != self._project.root
        ):
            return
        tabs = require_child(self._widget, QtWidgets.QTabWidget, "tabs_figures")
        payload: list[dict[str, object]] = []
        current_index: int | None = None
        for index in range(tabs.count()):
            page = tabs.widget(index)
            plot = self._plot_tabs.get(page)
            if plot is None:
                continue
            if page is tabs.currentWidget():
                current_index = len(payload)
            record = self._figure_tab_record(tabs, index)
            if record is not None:
                payload.append(record)
        # A closed view remains a project document and is restored on reopening.
        payload.extend(self._closed_figure_tabs)
        try:
            FigureTabsRepository(self._project.paths.state).save(payload, current_index)
        except (OSError, TypeError, ValueError):
            self._show_status("Failed to save figure tabs.")

    def _figure_tab_record(
        self, tabs: QtWidgets.QTabWidget, index: int
    ) -> dict[str, object] | None:
        page = tabs.widget(index)
        plot = self._plot_tabs.get(page)
        if plot is None:
            return None
        from FiguresArea.controllers.plot_tab_controller import PlotTabController
        from FiguresArea.controllers.snapshot_animation_tab_controller import AnimationTabController

        settings = FigureTabsRepository.settings_payload(plot.figure_settings())
        if isinstance(plot, AnimationTabController):
            settings["animation"] = plot.animation_state_payload()
        record: dict[str, object] = {
            "kind": str(page.property("figureKind")),
            "title": tabs.tabText(index),
            "settings": settings,
        }
        if isinstance(plot, PlotTabController):
            record["composition"] = FigureTabsRepository.composition_payload(
                plot.figure_composition()
            )
        return record

    def _schedule_project_tab_state_save(self) -> None:
        if (
            self._project is None
            or self._restoring_project_state
            or self._restored_project_root != self._project.root
        ):
            return
        self._state_save_timer.start()

    def _ensure_project_state_loaded(self) -> None:
        if self._project is None or self._restored_project_root == self._project.root:
            return
        self._clear_figure_tabs()
        self._restore_project_tab_state()
        self._restored_project_root = self._project.root

    def _restore_project_tab_state(self) -> None:
        if self._project is None:
            return
        try:
            records, current_index = FigureTabsRepository(self._project.paths.state).load()
        except OSError:
            self._show_status("Failed to restore figure tabs.")
            return
        self._restoring_project_state = True
        try:
            for record in records:
                self._add_figure_tab(
                    record["kind"],
                    record["title"],
                    record["settings"],
                    record.get("composition"),
                )
            if current_index is not None and 0 <= current_index < len(records):
                tabs = require_child(self._widget, QtWidgets.QTabWidget, "tabs_figures")
                pages = [
                    tabs.widget(index)
                    for index in range(tabs.count())
                    if tabs.widget(index) in self._plot_tabs
                ]
                if current_index < len(pages):
                    tabs.setCurrentWidget(pages[current_index])
        finally:
            self._restoring_project_state = False
        self._sync_settings_panel_with_current_tab()

    @staticmethod
    def _hide_add_tab_close_button(tabs: QtWidgets.QTabWidget) -> None:
        for index in range(tabs.count()):
            if tabs.widget(index).objectName() != "page_add_figure":
                continue
            tab_bar = tabs.tabBar()
            tab_bar.setTabButton(index, QtWidgets.QTabBar.ButtonPosition.LeftSide, None)
            tab_bar.setTabButton(index, QtWidgets.QTabBar.ButtonPosition.RightSide, None)
            break

    def _next_figure_number(self, tabs: QtWidgets.QTabWidget) -> int:
        count = 0
        for index in range(tabs.count()):
            if tabs.widget(index).objectName() != "page_add_figure":
                count += 1
        return count + len(self._closed_figure_tabs) + 1

    def _show_status(self, message: str) -> None:
        if self._status_callback is not None:
            self._status_callback(message)

    def _show_figure_settings_for_selection(self, selection_kind: str) -> None:
        if self._figure_settings_panel is not None:
            self._figure_settings_panel.show_page_for_selection(selection_kind)

    def _sync_settings_panel_with_current_tab(self) -> None:
        if self._figure_settings_panel is None:
            return
        tabs = self._widget.findChild(QtWidgets.QTabWidget, "tabs_figures")
        page = tabs.currentWidget() if tabs is not None else None
        plot_tab = self._plot_tabs.get(page) if page is not None else None
        figure_kind = str(page.property("figureKind") or "") if page is not None else ""
        if figure_kind in {"animation", "journal", "presentation"}:
            self._figure_settings_panel.set_active_plot_tab(plot_tab)
        else:
            self._figure_settings_panel.set_active_plot_tab(None)
        self._refresh_figure_settings_preset_menu()

    def _refresh_figure_settings_preset_menu(self) -> None:
        if self._figure_settings_panel is None:
            return
        self._figure_settings_panel.set_recent_preset_labels(
            [entry.label for entry in self._figure_settings_preset_entries()]
        )

    def _save_figure_settings_preset(self, label: str, payload: dict) -> bool:
        if self._project is None:
            return False
        try:
            PresetRepository(self._figure_settings_preset_root()).save_payload(
                label,
                payload,
                allow_overwrite=True,
            )
        except (OSError, ValueError):
            self._show_status("Failed to save figure settings preset.")
            return False
        self._refresh_figure_settings_preset_menu()
        return True

    def _load_figure_settings_preset_by_label(self, label: str) -> dict | None:
        if self._project is None:
            return None
        repository = PresetRepository(self._figure_settings_preset_root())
        for entry in repository.list_entries():
            if entry.label == label:
                return repository.load_payload_from_path(entry.path)
        return None

    def _figure_settings_preset_entries(self) -> list:
        if self._project is None:
            return []
        try:
            return PresetRepository(self._figure_settings_preset_root()).list_entries()
        except OSError:
            return []

    def _figure_settings_preset_root(self) -> Path:
        if self._project is None:
            return Path()
        return self._project.paths.figure_plot_settings_presets
