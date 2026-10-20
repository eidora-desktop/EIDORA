from __future__ import annotations

import weakref

from PySide6 import QtCore, QtWidgets


_FILTER_ATTRIBUTE = "_eidora_dialog_window_filter"
_OWNER_ATTRIBUTE = "_eidora_dialog_window_owner"
_EPHEMERAL_PROPERTY = "eidoraEphemeralWindow"


def set_dialog_window_owner(
    app: QtWidgets.QApplication,
    owner: QtWidgets.QWidget | None,
) -> None:
    """Set the application window that owns otherwise unparented dialogs."""

    setattr(app, _OWNER_ATTRIBUTE, weakref.ref(owner) if owner is not None else None)


def mark_ephemeral_window(widget: QtWidgets.QWidget) -> None:
    """Hide a short-lived top-level control when EIDORA loses focus."""

    widget.setProperty(_EPHEMERAL_PROPERTY, True)


class _DialogWindowFilter(QtCore.QObject):
    @staticmethod
    def _configured_owner(widget: QtWidgets.QWidget) -> QtWidgets.QWidget | None:
        parent = widget.parentWidget()
        if parent is not None:
            owner = parent.window()
            if owner is not widget:
                return owner

        app = QtWidgets.QApplication.instance()
        owner_ref = getattr(app, _OWNER_ATTRIBUTE, None) if app is not None else None
        return owner_ref() if callable(owner_ref) else None

    @classmethod
    def _attach_to_application_window(cls, widget: QtWidgets.QWidget) -> None:
        if not widget.isWindow():
            return
        owner = cls._configured_owner(widget)
        if owner is None or owner is widget:
            return

        owner_handle = owner.windowHandle()
        widget_handle = widget.windowHandle()
        if owner_handle is not None and widget_handle is not None:
            widget_handle.setTransientParent(owner_handle)

    @staticmethod
    def _hide_ephemeral_windows() -> None:
        app = QtWidgets.QApplication.instance()
        if app is None:
            return
        ephemeral_types = {
            QtCore.Qt.WindowType.Popup,
            QtCore.Qt.WindowType.ToolTip,
        }
        for widget in app.topLevelWidgets():
            if (
                widget.windowType() in ephemeral_types
                or bool(widget.property(_EPHEMERAL_PROPERTY))
            ):
                widget.hide()

    def eventFilter(
        self,
        watched: QtCore.QObject,
        event: QtCore.QEvent,
    ) -> bool:
        if (
            event.type() == QtCore.QEvent.Type.Show
            and isinstance(watched, QtWidgets.QDialog)
        ):
            self._attach_to_application_window(watched)
        elif event.type() == QtCore.QEvent.Type.ApplicationDeactivate:
            self._hide_ephemeral_windows()
        return False


def install_dialog_window_policy(app: QtWidgets.QApplication) -> None:
    """Keep transient EIDORA windows scoped to the application window."""

    if getattr(app, _FILTER_ATTRIBUTE, None) is not None:
        return
    event_filter = _DialogWindowFilter(app)
    app.installEventFilter(event_filter)
    setattr(app, _FILTER_ATTRIBUTE, event_filter)
