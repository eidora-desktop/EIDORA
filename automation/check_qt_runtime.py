"""Fail early when the build host lacks Qt GUI or multimedia libraries."""
from pathlib import Path
import sys

from PySide6 import QtCore, QtGui, QtWidgets, QtQuick, QtMultimedia, QtMultimediaWidgets


def check() -> None:
    if sys.platform == "linux":
        plugins = Path(QtCore.QLibraryInfo.path(QtCore.QLibraryInfo.LibraryPath.PluginsPath))
        xcb = QtCore.QLibrary(str(plugins / "platforms/libqxcb.so"))
        if not xcb.load():
            raise RuntimeError(f"Qt X11 platform dependencies are missing: {xcb.errorString()}")
    print(f"Qt {QtCore.qVersion()} GUI, Quick, and multimedia libraries loaded on {sys.platform}.")


if __name__ == "__main__":
    check()
