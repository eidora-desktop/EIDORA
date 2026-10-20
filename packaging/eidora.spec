# PyInstaller specification for the portable EIDORA application bundle.

from __future__ import annotations

import os
import re
import sys
import tomllib
from pathlib import Path
from PyInstaller.utils.hooks import collect_dynamic_libs


ROOT = Path(SPECPATH).resolve().parent
SRC = ROOT / "src"
MANIFEST_PATH = ROOT / "packaging" / "resource-manifest.toml"
NATIVE_STAGE = Path(os.environ["EIDORA_NATIVE_STAGE"]).resolve()
APP_ICON = os.environ.get("EIDORA_APP_ICON")

sys.path.insert(0, str(SRC))
sys.path.insert(0, str(ROOT / "automation"))
from eidora_version import __version__
from resource_inventory import release_resources
from prepare_macos_icon import validate_macos_icon


with MANIFEST_PATH.open("rb") as stream:
    manifest = tomllib.load(stream)

excluded = list(manifest["exclude"]["patterns"])


def excluded_resource(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    return any(relative.match(pattern) for pattern in excluded)


_, release_resource_paths, _ = release_resources(ROOT)
datas = [(str(ROOT / "CITATION.cff"), ".")]
if sys.platform == "darwin":
    macos_icons = ROOT / "packaging/icons/macos"
    validate_macos_icon(macos_icons)
    datas.append((str(macos_icons / "Assets.car"), "."))
    if not APP_ICON:
        APP_ICON = str(macos_icons / "eidora_icon.icns")
for source in sorted(release_resource_paths):
    if excluded_resource(source):
        continue
    relative = source.relative_to(SRC)
    datas.append((str(source), relative.parent.as_posix()))

from project.starter_collection import read_manifest
from project.starter_validation import sha256

starter = read_manifest(SRC / "project/resources/starter-collection.json")
starter_archive = Path(os.environ.get("EIDORA_STARTER_ARCHIVE", str(ROOT / "build/starter" / starter["archive"])))
if not starter_archive.is_file() or starter_archive.stat().st_size != starter["archive_bytes"] or sha256(starter_archive) != starter["sha256"]:
    raise SystemExit("Missing or invalid starter archive. Run automation/fetch_starter_collection.py first.")
datas.append((str(starter_archive), "project/resources"))

native_config = manifest["native_renderer"]
native_module = NATIVE_STAGE / native_config["stage_subdirectory"]
for required in native_config["required_files"]:
    if not (NATIVE_STAGE / required).is_file():
        raise SystemExit(f"Native renderer stage is incomplete: {required}")
native_destination = native_config["bundle_subdirectory"]
native_libraries = sorted(native_module.glob("*discovery_renderer.*"))
if not native_libraries:
    raise SystemExit(f"Native renderer library is missing: {native_module}")

binaries = [(str(library), native_destination) for library in native_libraries]
binaries.extend(collect_dynamic_libs("av"))
datas.append((str(native_module / "qmldir"), native_destination))

analysis = Analysis(
    [str(SRC / "app.py")],
    pathex=[str(SRC)],
    binaries=binaries,
    datas=datas,
    hiddenimports=list(manifest["dynamic_imports"]["modules"]),
    hookspath=[],
    # Figure exports select their writer backend from the file extension at
    # runtime. PyInstaller cannot infer those imports from ``savefig()``, so
    # keep every self-contained backend advertised by the export dialog.
    hooksconfig={"matplotlib": {"backends": ["QtAgg", "Agg", "pdf", "ps", "svg"]}},
    runtime_hooks=[],
    excludes=[
        "PySide6.Qt3D",
        "PySide6.QtBluetooth",
        "PySide6.QtCharts",
        "PySide6.QtDataVisualization",
        "PySide6.QtGraphs",
        "PySide6.QtLocation",
        "PySide6.QtPdf",
        "PySide6.QtPositioning",
        "PySide6.QtRemoteObjects",
        "PySide6.QtScxml",
        "PySide6.QtSensors",
        "PySide6.QtTextToSpeech",
        "PySide6.QtWebChannel",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineQuick",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebSockets",
        "PySide6.QtWebView",
        "_pytest",
        "pytest",
        "pyarrow.flight",
        "pyarrow._flight",
    ],
    noarchive=False,
    optimize=1,
)

# PyInstaller's QtQml hook deliberately collects every QML module shipped by
# PySide. EIDORA's two QML files use only the QtQuick core module. Keeping that
# broad collection would add WebEngine, QtQuick3D, multimedia, and other
# unrelated frameworks to every installer.
_QML_ROOT = "PySide6/Qt/qml/"
_QML_DIRECTORIES = {
    "QtCore",
    "QtQml",
    "QtQml/Models",
    "QtQml/WorkerScript",
    "QtQuick",
    "QtQuick/Window",
}

_QT_FRAMEWORKS = {
    "QtConcurrent",
    "QtCore",
    "QtDBus",
    "QtGui",
    "QtMultimedia",
    "QtMultimediaWidgets",
    "QtNetwork",
    "QtOpenGL",
    "QtOpenGLWidgets",
    "QtQml",
    "QtQmlMeta",
    "QtQmlModels",
    "QtQmlNetwork",
    "QtQmlWorkerScript",
    "QtQuick",
    "QtQuickWidgets",
    "QtSvg",
    "QtUiTools",
    "QtWidgets",
    # libqxcb.so needs this library to create the Linux application window.
    "QtXcbQpa",
}

_QT_PLUGIN_DIRECTORIES = {
    "generic",
    "iconengines",
    "imageformats",
    "multimedia",
    "networkinformation",
    "platforminputcontexts",
    "platforms",
    "styles",
    "tls",
    "xcbglintegrations",
}


def _keep_qt_entry(entry):
    destination = entry[0].replace("\\", "/")
    # Windows wheels put DLLs, qml, plugins and resources directly under
    # PySide6; Unix wheels use PySide6/Qt. Apply one policy to both layouts.
    if destination.startswith("PySide6/") and not destination.startswith("PySide6/Qt/"):
        destination = destination.replace("PySide6/", "PySide6/Qt/", 1)
    top_level_name = destination.split("/", 1)[0]
    if top_level_name.startswith("Qt") and "." not in top_level_name and top_level_name not in _QT_FRAMEWORKS:
        return False
    if _QML_ROOT in destination:
        relative = destination.split(_QML_ROOT, 1)[1]
        directory = relative.rsplit("/", 1)[0] if "/" in relative else ""
        if directory not in _QML_DIRECTORIES:
            return False

    framework_marker = "PySide6/Qt/lib/"
    if framework_marker in destination and ".framework/" in destination:
        framework = destination.split(framework_marker, 1)[1].split(".framework/", 1)[0]
        if framework.startswith("Qt") and framework not in _QT_FRAMEWORKS:
            return False

    library_patterns = (
        r"(?:^|/)Qt6([A-Za-z0-9]+)\.dll$",
        # Apply the same library policy to PyInstaller's top-level aliases.
        r"(?:^|/)libQt6([A-Za-z0-9]+)\.so(?:\..*)?$",
        r"PySide6/Qt/lib/libQt6([A-Za-z0-9]+)\.dylib$",
    )
    for pattern in library_patterns:
        match = re.search(pattern, destination)
        if match and f"Qt{match.group(1)}" not in _QT_FRAMEWORKS:
            return False

    if destination.rsplit("/", 1)[-1] in {"QtWebEngineProcess", "QtWebEngineProcess.exe"}:
        return False
    if "pyside6/qt/resources/qtwebengine" in destination.lower():
        return False

    plugin_marker = "PySide6/Qt/plugins/"
    if plugin_marker in destination:
        plugin_directory = destination.split(plugin_marker, 1)[1].split("/", 1)[0]
        if plugin_directory not in _QT_PLUGIN_DIRECTORIES:
            return False
    return True


# PyArrow's hook also collects its optional Flight RPC server/client libraries.
# EIDORA uses local Arrow IPC and Parquet files; Flight adds over 20 MiB without
# serving any import/export format. Keep the existing application size budget.
analysis.binaries = [
    entry for entry in analysis.binaries
    if _keep_qt_entry(entry)
    # PyInstaller can also add top-level aliases for excluded Flight dylibs.
    and "arrow_flight" not in Path(entry[0]).name.lower()
    and not (entry[0].replace("\\", "/").startswith("pyarrow/")
             and "flight" in Path(entry[0]).name.lower())
]
analysis.datas = [
    entry for entry in analysis.datas
    if _keep_qt_entry(entry)
    and not (entry[2] == "SYMLINK" and "arrow_flight" in Path(entry[0]).name.lower())
]
pyz = PYZ(analysis.pure)

executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="EIDORA",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=APP_ICON,
)
collection = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="EIDORA",
)

if sys.platform == "darwin":
    application = BUNDLE(
        collection,
        name="EIDORA.app",
        icon=APP_ICON,
        bundle_identifier="app.eidora.desktop",
        version=__version__,
        info_plist={
            "CFBundleDisplayName": "EIDORA",
            "CFBundleShortVersionString": __version__,
            "CFBundleIconName": "EIDORA",
            "NSHighResolutionCapable": True,
        },
    )
