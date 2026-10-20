from __future__ import annotations

import sys
from pathlib import Path

from shared.env import get_env


WORKSPACE_ROOT_ENV_VAR = "EIDORA_WORKSPACE_ROOT"


def application_root() -> Path:
    """Return the repository root when running from a source checkout."""
    return Path(__file__).resolve().parents[2]


def is_source_checkout() -> bool:
    """Return whether this process is running from an EIDORA source tree."""
    if getattr(sys, "frozen", False):
        return False
    root = application_root()
    return (root / "pyproject.toml").is_file() and (root / "src" / "app.py").is_file()


def application_data_root() -> Path:
    """Return the writable, platform-native EIDORA application-data folder."""
    try:
        from PySide6 import QtCore

        location = QtCore.QStandardPaths.writableLocation(
            QtCore.QStandardPaths.StandardLocation.AppDataLocation
        )
        if location and not QtCore.QCoreApplication.applicationName():
            location = str(Path(location) / "EIDORA" / "EIDORA Desktop")
    except Exception:
        location = ""
    return Path(location) if location else user_home() / ".eidora"


def default_workspace_root() -> Path:
    """Return the project workspace without depending on the launch directory.

    ``EIDORA_WORKSPACE_ROOT`` lets packaged installs and developers keep project
    data outside a checkout.  Source checkouts otherwise retain their existing
    ``workspace_projects`` folder when the checkout is moved or launched by an
    IDE from another working directory.
    """
    configured_root = get_env(WORKSPACE_ROOT_ENV_VAR).strip()
    if configured_root:
        return Path(configured_root).expanduser()
    if is_source_checkout():
        return application_root() / "workspace_projects"
    if getattr(sys, "frozen", False):
        from shared.windows_package import current_package_full_name

        if current_package_full_name():
            # AppData is virtualized and removed on MSIX uninstall. User projects
            # and the starter receipt must outlive the application package.
            return user_home() / "EIDORA" / "workspace_projects"
    return application_data_root() / "workspace_projects"


def user_home() -> Path:
    return Path.home()
