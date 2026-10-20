from __future__ import annotations

from DiscoveryArea.controllers.layers_setting_panel_controller import LayersSettingPanelController
from DiscoveryArea.controllers.appearance_panel_controller import AppearancePanelController
from DiscoveryArea.controllers.canvas_layers_panel_controller import CanvasLayersPanelController
from DiscoveryArea.controllers.canvas_tools_panel_controller import CanvasToolsPanelController
from DiscoveryArea.controllers.filters_panel_controller import FiltersPanelController
from DiscoveryArea.controllers.main_controller import DiscoveryAreaController
from DiscoveryArea.controllers.discovery_panel_controller import DiscoveryPanelController
from DiscoveryArea.controllers.slicer_panel_controller import SlicerPanelController
from DiscoveryArea.controllers.snapshots_panel_controller import SnapshotsPanelController
from DiscoveryArea.controllers.transform_panel_controller import TransformPanelController
from DiscoveryArea.controllers.selection_actions_dialog_controller import (
    SelectionActionsDialogData,
    SelectionActionsDialogController,
    SelectionActionsResult,
)

ProjectionsAreaController = DiscoveryAreaController
ProjectionPanelController = DiscoveryPanelController

__all__ = [
    "LayersSettingPanelController",
    "AppearancePanelController",
    "CanvasLayersPanelController",
    "CanvasToolsPanelController",
    "FiltersPanelController",
    "DiscoveryAreaController",
    "DiscoveryPanelController",
    "ProjectionsAreaController",
    "ProjectionPanelController",
    "SlicerPanelController",
    "SnapshotsPanelController",
    "TransformPanelController",
    "SelectionActionsDialogData",
    "SelectionActionsDialogController",
    "SelectionActionsResult",
]
