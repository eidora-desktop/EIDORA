from __future__ import annotations

from collections.abc import Callable

from PySide6 import QtWidgets


def require_child(parent: QtWidgets.QWidget, widget_type: type, object_name: str):
    widget = parent.findChild(widget_type, object_name)
    if widget is None:
        raise RuntimeError(f"{parent.objectName() or parent.__class__.__name__} is missing {object_name!r}")
    return widget


def add_widget_to_container(
    container: QtWidgets.QWidget,
    widget: QtWidgets.QWidget,
) -> None:
    if container.layout() is None:
        layout = QtWidgets.QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
    container.layout().addWidget(widget)


def connect_placeholder_buttons(
    parent: QtWidgets.QWidget,
    on_triggered: Callable[[str], None],
) -> None:
    for button in parent.findChildren(QtWidgets.QAbstractButton):
        if button.property("hasRealAction"):
            continue
        text = button.text().strip() or button.objectName()
        if text:
            button.clicked.connect(lambda _checked=False, label=text: on_triggered(label))


def wire_page_buttons(
    parent: QtWidgets.QWidget,
    stack_name: str,
    button_names: list[str],
) -> None:
    stack = parent.findChild(QtWidgets.QStackedWidget, stack_name)
    if stack is None:
        return

    group = QtWidgets.QButtonGroup(parent)
    group.setExclusive(True)
    for index, button_name in enumerate(button_names):
        button = parent.findChild(QtWidgets.QAbstractButton, button_name)
        if button is None:
            continue
        button.setCheckable(True)
        group.addButton(button, index)
        button.clicked.connect(lambda _checked=False, page=index: stack.setCurrentIndex(page))
        if index == 0:
            button.setChecked(True)
            stack.setCurrentIndex(0)


def wire_two_option_stack(
    parent: QtWidgets.QWidget,
    stack_name: str,
    first_button_name: str,
    second_button_name: str,
) -> None:
    stack = parent.findChild(QtWidgets.QStackedWidget, stack_name)
    first = parent.findChild(QtWidgets.QAbstractButton, first_button_name)
    second = parent.findChild(QtWidgets.QAbstractButton, second_button_name)
    if stack is None or first is None or second is None:
        return

    group = QtWidgets.QButtonGroup(parent)
    group.setExclusive(True)
    group.addButton(first, 0)
    group.addButton(second, 1)
    first.toggled.connect(lambda checked: stack.setCurrentIndex(0) if checked else None)
    second.toggled.connect(lambda checked: stack.setCurrentIndex(1) if checked else None)
    first.setChecked(True)
    stack.setCurrentIndex(0)
