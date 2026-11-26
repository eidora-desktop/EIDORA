"""Native file dialogs with independent, installation-local import/export folders."""
from pathlib import Path

from PySide6 import QtWidgets

from application_settings.session_repository import SessionPreferences


def suggested_path(direction: str, default: str = "", *, directory: bool = False) -> str:
    saved = SessionPreferences().read("file_dialogs").get(direction)
    if not isinstance(saved, str) or not saved or not Path(saved).is_dir():
        return default
    name = "" if directory or not default or Path(default).is_dir() else Path(default).name
    return str(Path(saved) / name)


def remember_path(direction: str, selected: str, *, directory: bool = False) -> None:
    if selected:
        path = Path(selected).expanduser().absolute()
        SessionPreferences().update("file_dialogs", **{direction: str(path if directory else path.parent)})


def get_open_file_name(parent=None, caption="", directory="", *args, **kwargs):
    result = QtWidgets.QFileDialog.getOpenFileName(
        parent, caption, suggested_path("import", directory), *args, **kwargs,
    )
    remember_path("import", result[0])
    return result


def get_open_file_names(parent=None, caption="", directory="", *args, **kwargs):
    result = QtWidgets.QFileDialog.getOpenFileNames(
        parent, caption, suggested_path("import", directory), *args, **kwargs,
    )
    if result[0]:
        remember_path("import", result[0][0])
    return result


def get_import_directory(parent=None, caption="", directory="", *args, **kwargs):
    result = QtWidgets.QFileDialog.getExistingDirectory(
        parent, caption, suggested_path("import", directory, directory=True), *args, **kwargs,
    )
    remember_path("import", result, directory=True)
    return result


def get_save_file_name(parent=None, caption="", directory="", *args, use_saved_directory=True, **kwargs):
    result = QtWidgets.QFileDialog.getSaveFileName(
        parent, caption, suggested_path("export", directory) if use_saved_directory else directory,
        *args, **kwargs,
    )
    remember_path("export", result[0])
    return result
