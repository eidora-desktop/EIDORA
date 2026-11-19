from collections.abc import Callable

from PySide6 import QtWidgets

from shared.qt.deferred import defer_to_event_loop


def append_manage_presets_action(
    menu: QtWidgets.QMenu, open_settings: Callable[[str], None]
) -> None:
    """End a preset list with a shortcut to the Settings Presets tab."""
    menu.addSeparator()
    action = menu.addAction("Manage Presets...")
    action.triggered.connect(
        lambda _checked=False: defer_to_event_loop(lambda: open_settings("presets"))
    )
