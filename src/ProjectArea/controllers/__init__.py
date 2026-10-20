"""Project-area controller exports without eager feature imports.

The main window imports this package during application startup.  Keep the
individual controller modules lazy so optional processing dependencies are
loaded only when their controller is requested.
"""

from importlib import import_module
from typing import Any

__all__ = [
    "ProjectAreaController",
    "MediaSourcesPanelController",
    "ThumbnailsPanelController",
]


_CONTROLLER_MODULES = {
    "ProjectAreaController": "ProjectArea.controllers.main_controller",
    "MediaSourcesPanelController": "ProjectArea.controllers.media_sources_panel_controller",
    "ThumbnailsPanelController": "ProjectArea.controllers.thumbnails_panel_controller",
}


def __getattr__(name: str) -> Any:
    module_name = _CONTROLLER_MODULES.get(name)
    if module_name is None:
        raise AttributeError(name)
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
