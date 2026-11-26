"""Default Qt logging policy for the desktop application."""

from PySide6 import QtCore


def configure_multimedia_logging() -> None:
    # The FFmpeg backend gates its per-file metadata dump on the info level.
    # Keep warning/critical messages so failed playback remains diagnosable.
    QtCore.QLoggingCategory.setFilterRules(
        "qt.multimedia*.debug=false\nqt.multimedia*.info=false"
    )
