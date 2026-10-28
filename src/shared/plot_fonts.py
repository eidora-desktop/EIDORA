"""Bounded font discovery for Matplotlib, initialized only when plotting starts."""

from __future__ import annotations

from functools import cache
import logging
import os
from pathlib import Path
import subprocess
import sys
import time


BUNDLED_FONT_FAMILIES = (
    "DejaVu Sans", "DejaVu Serif", "DejaVu Sans Mono", "STIXGeneral",
)
SYSTEM_FONT_WISHLIST = (
    "Arial", "Helvetica", "Times New Roman", "Times", "Courier New", "Courier",
    "Helvetica Neue", "Verdana", "Futura", "Calibri", "Aptos", "Tahoma",
    "Trebuchet MS", "Gill Sans", "Georgia", "Cambria", "Palatino",
    "Palatino Linotype", "Garamond", "Consolas", "Symbol", "Cambria Math",
)
_LOGGER = logging.getLogger(__name__)


@cache
def initialize_plot_fonts() -> tuple[str, ...]:
    """Load bundled fonts and verified wishlist files once per process.

    Matplotlib >= 3.11 can initialize without enumerating system fonts. Remove
    that restriction after initialization so explicitly registered OS fonts can
    be selected too. This must run before importing any Matplotlib backends.
    """
    os.environ["MPL_IGNORE_SYSTEM_FONTS"] = "1"
    try:
        import matplotlib
        from matplotlib import font_manager
    finally:
        os.environ.pop("MPL_IGNORE_SYSTEM_FONTS", None)

    # Some OS faces (e.g. macOS Courier Oblique) have native hinting instructions
    # that FreeType cannot execute at figure sizes. Use its automatic hinter.
    matplotlib.rcParams["text.hinting"] = "auto"
    manager = font_manager.fontManager
    bundled_root = Path(matplotlib.get_data_path()) / "fonts"
    # Older caches can include the complete system inventory, stale paths, or
    # another installation. Retain only this runtime's bundled font files.
    manager.ttflist[:] = [
        entry for entry in manager.ttflist
        if Path(entry.fname).is_relative_to(bundled_root) and Path(entry.fname).is_file()
    ]
    manager.afmlist[:] = [
        entry for entry in manager.afmlist
        if Path(entry.fname).is_relative_to(bundled_root) and Path(entry.fname).is_file()
    ]
    # Invalidate matches too when plotting was imported by an embedding host.
    manager._findfont_cached.cache_clear()
    if not manager.ttflist:
        # A cache from another environment may contain no usable bundled paths.
        for path in sorted((bundled_root / "ttf").glob("*.ttf")):
            manager.addfont(path)

    for path in sorted(set(_system_font_paths())):
        if not path.is_file():
            continue
        try:
            manager.addfont(path)
        except (OSError, RuntimeError, ValueError) as error:
            _LOGGER.debug("Cannot load plot font %s: %s", path, error)
    allowed = set(BUNDLED_FONT_FAMILIES + SYSTEM_FONT_WISHLIST)
    manager.ttflist[:] = [
        entry for entry in manager.ttflist
        if entry.name in allowed or Path(entry.fname).is_relative_to(bundled_root)
    ]
    manager._findfont_cached.cache_clear()
    # Font collections can also contain unrelated families. Check the actual
    # font metadata, never findfont's automatic fallback, for menu availability.
    available = {entry.name for entry in manager.ttflist} & allowed
    return tuple(
        family for family in BUNDLED_FONT_FAMILIES + SYSTEM_FONT_WISHLIST
        if family in available
    )


def _system_font_paths() -> list[Path]:
    if sys.platform == "darwin":
        return _macos_font_paths()
    if sys.platform == "win32":
        return _windows_font_paths()
    return _fontconfig_font_paths()


def _macos_font_paths() -> list[Path]:
    """Probe standard filenames directly, including local and user installs."""
    roots = (
        Path("/System/Library/Fonts"),
        Path("/System/Library/Fonts/Supplemental"),
        Path("/Library/Fonts"),
        Path("/Library/Fonts/Microsoft"),
        Path.home() / "Library/Fonts",
        Path.home() / "Library/Fonts/Microsoft",
    )
    styles = (
        "", " Bold", " Italic", " Bold Italic", " Oblique", " Bold Oblique",
        "-Regular", "-Bold", "-Italic", "-BoldItalic", "-Oblique", "-BoldOblique",
    )
    paths = []
    for family in SYSTEM_FONT_WISHLIST:
        for stem in dict.fromkeys((family, family.replace(" ", ""))):
            for style in styles:
                for extension in (".ttf", ".otf", ".ttc"):
                    for root in roots:
                        path = root / f"{stem}{style}{extension}"
                        if path.is_file():
                            paths.append(path)
    return paths


def _windows_font_paths() -> list[Path]:
    """Read matching registry records without opening every installed font."""
    import winreg

    system_root = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    local_root = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "Microsoft/Windows/Fonts"
    key_name = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
    paths = []
    for hive, root in ((winreg.HKEY_CURRENT_USER, local_root), (winreg.HKEY_LOCAL_MACHINE, system_root)):
        try:
            with winreg.OpenKey(hive, key_name) as key:
                for index in range(winreg.QueryInfoKey(key)[1]):
                    name, value, _kind = winreg.EnumValue(key, index)
                    if not isinstance(value, str) or not _wanted_registry_font(name):
                        continue
                    path = Path(os.path.expandvars(value))
                    paths.append(path if path.is_absolute() else root / path)
        except OSError:
            continue
    return paths


def _wanted_registry_font(name: str) -> bool:
    # Collections may use e.g. "Cambria & Cambria Math (TrueType)".
    names = name.split(" (")[0].split(" & ")
    styles = ("", " Regular", " Bold", " Italic", " Bold Italic", " Oblique", " Bold Oblique")
    return any(
        value.casefold() == f"{family}{style}".casefold()
        for value in names for family in SYSTEM_FONT_WISHLIST for style in styles
    )


def _fontconfig_font_paths() -> list[Path]:
    """Query exact families in cached fontconfig metadata, within one second."""
    deadline = time.monotonic() + 1.0
    paths = []
    # fc-list treats comma-separated family values as AND, not alternatives.
    for family in SYSTEM_FONT_WISHLIST:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            result = subprocess.run(
                ["fc-list", "--format=%{file}\\n", ":family=" + family],
                capture_output=True, text=True, check=True, timeout=remaining,
            )
        except (OSError, subprocess.SubprocessError):
            break
        paths.extend(Path(line) for line in result.stdout.splitlines() if line.strip())
    return paths
