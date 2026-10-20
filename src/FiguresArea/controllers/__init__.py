from __future__ import annotations

from FiguresArea.controllers.animation_export_dialog_controller import (
    AnimationExportDialogController,
)
from FiguresArea.controllers.figure_settings_panel_controller import FigureSettingsPanelController
from FiguresArea.controllers.main_controller import FiguresAreaController
from FiguresArea.controllers.snapshot_sources_panel_controller import SnapshotSourcesPanelController

__all__ = [
    "AnimationExportDialogController",
    "AnimationTabController",
    "SnapshotAnimationTabController",
    "FiguresAreaController",
    "FigureSettingsPanelController",
    "SnapshotSourcesPanelController",
]


def __getattr__(name: str):
    # Animation tabs pull in Matplotlib. Merely opening a project must not
    # initialize plotting or scan system fonts.
    if name not in {"AnimationTabController", "SnapshotAnimationTabController"}:
        raise AttributeError(name)
    from FiguresArea.controllers import snapshot_animation_tab_controller

    value = getattr(snapshot_animation_tab_controller, name)
    globals()[name] = value
    return value
