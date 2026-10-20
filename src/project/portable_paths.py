"""Project-local paths stay relative on disk and absolute at runtime."""
from pathlib import Path, PureWindowsPath


class ProjectPaths:
    def __init__(self, root: Path | None):
        self.root = Path(root).resolve() if root is not None else None

    def encode(self, value: Path | str | None) -> str:
        if not value:
            return ""
        text = str(value)
        if self.root is None:
            return text
        path = Path(text)
        if not path.is_absolute():
            if PureWindowsPath(text).is_absolute():
                return text
            return text.replace("\\", "/")
        try:
            return path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return text

    def decode(self, value: Path | str | None) -> str:
        if not value:
            return ""
        text = str(value)
        if self.root is None or Path(text).is_absolute() or PureWindowsPath(text).is_absolute():
            return text
        return str(self.root.joinpath(*text.replace("\\", "/").split("/")).resolve())

    def key(self, value: str, *, encode: bool = False) -> str:
        if not value or value == "random" or value.startswith("run:") or not any(separator in value for separator in ("/", "\\")):
            return value
        return self.encode(value) if encode else self.decode(value)


def repository_paths(database: Path, root: Path | None, directory: str) -> ProjectPaths:
    # Retain the existing constructor API for callers using canonical stores.
    if root is None and Path(database).parent.name == directory:
        root = Path(database).parent.parent
    return ProjectPaths(root)
