from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from PySide6 import QtCore, QtGui, QtWidgets


PAN_OPEN = "pan_open"
PAN_CLOSED = "pan_closed"
ZOOM_IN = "zoom_in"
ZOOM_OUT = "zoom_out"
LASSO_ADD_POINT = "lasso_add_point"
LASSO_CLOSE_POINT = "lasso_close_point"
LASSO_REMOVE_POINT = "lasso_remove_point"
LASSO_PEN_NORMAL = "lasso_pen_normal"
LASSO_SELECT_AREA = "lasso_select_area"
LASSO_MOVE_ANCHOR = "lasso_move_anchor"
LASSO_ADD_ANCHOR = "lasso_add_anchor"
LASSO_DELETE_AREA = "lasso_delete_area"
PIN_ADD = "pin_add"
PIN_MODE = "pin_mode"
PIN_MOVE = "pin_move"
PIN_REMOVE = "pin_remove"
SELECTION_ADD = "selection_add"
SELECTION_MODE = "selection_mode"


@dataclass(frozen=True)
class CursorSpec:
    asset_name: str
    hotspot: tuple[int, int]
    fallback: QtCore.Qt.CursorShape


CURSOR_SPECS: dict[str, CursorSpec] = {
    PAN_OPEN: CursorSpec("hand_open", (23, 23), QtCore.Qt.CursorShape.OpenHandCursor),
    PAN_CLOSED: CursorSpec("hand_closed", (23, 23), QtCore.Qt.CursorShape.ClosedHandCursor),
    ZOOM_IN: CursorSpec("zoom_in", (24, 24), QtCore.Qt.CursorShape.SizeFDiagCursor),
    ZOOM_OUT: CursorSpec("zoom_out", (24, 24), QtCore.Qt.CursorShape.SizeBDiagCursor),
    LASSO_ADD_POINT: CursorSpec("add_point", (23, 23), QtCore.Qt.CursorShape.CrossCursor),
    LASSO_CLOSE_POINT: CursorSpec("close_point", (23, 23), QtCore.Qt.CursorShape.CrossCursor),
    LASSO_REMOVE_POINT: CursorSpec("remove_point", (23, 23), QtCore.Qt.CursorShape.CrossCursor),
    LASSO_PEN_NORMAL: CursorSpec("pen_normal", (23, 23), QtCore.Qt.CursorShape.CrossCursor),
    LASSO_SELECT_AREA: CursorSpec("pen_normal", (23, 23), QtCore.Qt.CursorShape.PointingHandCursor),
    LASSO_MOVE_ANCHOR: CursorSpec("move_point", (23, 23), QtCore.Qt.CursorShape.OpenHandCursor),
    LASSO_ADD_ANCHOR: CursorSpec("add_point", (23, 23), QtCore.Qt.CursorShape.CrossCursor),
    LASSO_DELETE_AREA: CursorSpec("remove_point", (23, 23), QtCore.Qt.CursorShape.CrossCursor),
    PIN_ADD: CursorSpec("add_pin", (22, 22), QtCore.Qt.CursorShape.CrossCursor),
    PIN_MODE: CursorSpec("pin_mode", (22, 22), QtCore.Qt.CursorShape.CrossCursor),
    PIN_MOVE: CursorSpec("pin_move", (22, 22), QtCore.Qt.CursorShape.OpenHandCursor),
    PIN_REMOVE: CursorSpec("remove_pin", (22, 22), QtCore.Qt.CursorShape.CrossCursor),
    SELECTION_ADD: CursorSpec(
        "add_selection",
        (23, 23),
        QtCore.Qt.CursorShape.PointingHandCursor,
    ),
    SELECTION_MODE: CursorSpec(
        "selection_mode",
        (23, 23),
        QtCore.Qt.CursorShape.PointingHandCursor,
    ),
}

_CURSOR_DIRECTORY = Path(__file__).resolve().parent


@lru_cache(maxsize=None)
def cursor_for(name: str) -> QtGui.QCursor:
    """Return a cached cursor configured by its semantic name."""
    try:
        spec = CURSOR_SPECS[name]
    except KeyError as error:
        raise ValueError(f"Unknown cursor: {name}") from error
    pixmap = QtGui.QPixmap(str(_CURSOR_DIRECTORY / f"{spec.asset_name}.svg"))
    if pixmap.isNull():
        return QtGui.QCursor(spec.fallback)
    return QtGui.QCursor(pixmap, spec.hotspot[0], spec.hotspot[1])


CursorTarget = QtWidgets.QWidget | QtGui.QWindow


def set_cursor(name: str, widgets: Iterable[CursorTarget]) -> None:
    """Assign one semantic cursor to stable widget/viewport owners."""
    cursor = cursor_for(name)
    for widget in widgets:
        widget.setCursor(cursor)


class CanvasCursorController(QtCore.QObject):
    """Keep one semantic cursor authoritative across QWidget and Qt Quick."""

    _RECOVERY_EVENTS = {
        QtCore.QEvent.Type.Enter,
        QtCore.QEvent.Type.GrabMouse,
        QtCore.QEvent.Type.UngrabMouse,
        QtCore.QEvent.Type.WindowActivate,
    }

    def __init__(self, parent: QtCore.QObject | None = None) -> None:
        self._targets: list[CursorTarget] = []
        self._target_ids: tuple[int, ...] = ()
        self._cursor_name: str | None = None
        self._applying = False
        self._reapply_scheduled = False
        super().__init__(parent)

    @property
    def cursor_name(self) -> str | None:
        return self._cursor_name

    def set_targets(self, targets: Iterable[CursorTarget]) -> None:
        unique_targets: list[CursorTarget] = []
        seen: set[int] = set()
        for target in targets:
            identity = id(target)
            if identity in seen:
                continue
            seen.add(identity)
            unique_targets.append(target)
        target_ids = tuple(id(target) for target in unique_targets)
        if target_ids == self._target_ids:
            return
        for target in self._targets:
            target.removeEventFilter(self)
        self._targets = unique_targets
        self._target_ids = target_ids
        for target in self._targets:
            target.installEventFilter(self)
        if self._cursor_name is not None:
            self.apply(force=True)

    def set_cursor(self, name: str, *, force: bool = False) -> None:
        changed = name != self._cursor_name
        self._cursor_name = name
        targets_match = all(self._has_desired_cursor(target) for target in self._targets)
        if changed or force or not targets_match:
            self.apply(force=True)

    def apply(self, *, force: bool = False) -> None:
        del force
        if self._cursor_name is None or self._applying:
            return
        self._applying = True
        try:
            set_cursor(self._cursor_name, self._targets)
        finally:
            self._applying = False

    def eventFilter(self, watched: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if getattr(self, "_cursor_name", None) is None or getattr(self, "_applying", False):
            return False
        if event.type() == QtCore.QEvent.Type.CursorChange:
            if isinstance(watched, (QtWidgets.QWidget, QtGui.QWindow)) and not self._has_desired_cursor(watched):
                self._schedule_reapply()
        elif event.type() in self._RECOVERY_EVENTS:
            self._schedule_reapply()
        return False

    def _schedule_reapply(self) -> None:
        if self._reapply_scheduled:
            return
        self._reapply_scheduled = True
        QtCore.QTimer.singleShot(0, self._reapply)

    def _reapply(self) -> None:
        self._reapply_scheduled = False
        self.apply(force=True)

    def _has_desired_cursor(self, target: CursorTarget) -> bool:
        if self._cursor_name is None:
            return True
        expected = cursor_for(self._cursor_name)
        actual = target.cursor()
        if actual.shape() != expected.shape() or actual.hotSpot() != expected.hotSpot():
            return False
        if actual.shape() != QtCore.Qt.CursorShape.BitmapCursor:
            return True
        return actual.pixmap().cacheKey() == expected.pixmap().cacheKey()
