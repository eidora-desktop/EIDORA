"""Bind the two Designer-owned text appearance controls."""
from PySide6 import QtCore, QtGui, QtWidgets

from DiscoveryArea.domain import DiscoveryPinDisplaySettings
from shared.plot_fonts import BUNDLED_FONT_FAMILIES, SYSTEM_FONT_WISHLIST
from shared.qt.widgets import require_child


class TextPreviewSettingsController(QtCore.QObject):
    changed = QtCore.Signal(dict)

    def __init__(self, root, prefix="tooltip"):
        self._prefix = prefix
        super().__init__(root)
        self.panel = require_child(root, QtWidgets.QWidget, f"panel_{prefix}_text_settings")
        self._font = require_child(root, QtWidgets.QComboBox, f"combo_{prefix}_font")
        self._size = require_child(root, QtWidgets.QSpinBox, f"spin_{prefix}_text_size")
        available = set(QtGui.QFontDatabase.families())
        self._font_items = [("Application font", "")] + [
            (family, family) for family in BUNDLED_FONT_FAMILIES + SYSTEM_FONT_WISHLIST
            if family in available
        ]
        self._font.currentIndexChanged.connect(self._changed)
        self._size.valueChanged.connect(self._changed)
        self.apply({})
        self.panel.hide()

    def apply(self, payload):
        settings = DiscoveryPinDisplaySettings.from_payload(payload)
        family = getattr(settings, f"{self._prefix}_text_font")
        size = getattr(settings, f"{self._prefix}_text_size")
        with QtCore.QSignalBlocker(self._font), QtCore.QSignalBlocker(self._size):
            items = list(self._font_items)
            if family and family not in dict(items):
                items.append((family, family))
            if [self._font.itemData(i) for i in range(self._font.count())] != [value for _, value in items]:
                self._font.clear()
                for label, value in items:
                    self._font.addItem(label, value)
            self._font.setCurrentIndex(self._font.findData(family))
            self._size.setValue(size)

    def _changed(self, *_args):
        self.changed.emit({
            f"{self._prefix}_text_font": self._font.currentData() or "",
            f"{self._prefix}_text_size": self._size.value(),
        })

