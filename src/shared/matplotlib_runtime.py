"""Persistent font caching for installed applications, without importing plotting."""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
import sys


def configure_matplotlib_cache() -> None:
    """Replace PyInstaller's per-process cache before the first plotting import.

    The automatic PyInstaller hook has already run when app.main starts. Cache
    separately by release and bundle location so upgrades or moved installations
    cannot reuse font paths from a different bundle. Source runs retain their
    normal Matplotlib configuration.
    """
    if not getattr(sys, "frozen", False):
        return

    from PySide6 import QtCore
    from eidora_version import __version__

    cache_root = QtCore.QStandardPaths.writableLocation(
        QtCore.QStandardPaths.StandardLocation.GenericCacheLocation
    )
    if not cache_root:
        return
    # _MEIPASS identifies the actual resource root, including transient bundles.
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)).resolve()
    location_key = hashlib.sha256(str(bundle_root).encode("utf-8")).hexdigest()[:16]
    cache = Path(cache_root) / "EIDORA" / "matplotlib" / f"{__version__}-{location_key}"
    try:
        cache.mkdir(parents=True, exist_ok=True)
        # Check writing too: an existing cache may have become read-only.
        from tempfile import TemporaryFile

        with TemporaryFile(dir=cache):
            pass
    except OSError:
        logging.getLogger(__name__).warning("Cannot use persistent Matplotlib cache: %s", cache)
        return
    os.environ["MPLCONFIGDIR"] = str(cache)
