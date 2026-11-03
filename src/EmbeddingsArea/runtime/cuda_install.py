"""Install pinned NVIDIA runtime wheels without pip or modifying the application."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import platform
import shutil
import ssl
import stat
import sys
import tempfile
from urllib.parse import urlparse
from urllib.request import urlopen
import zipfile

from filelock import FileLock, Timeout


class InstallCancelled(RuntimeError):
    pass


def platform_key() -> str | None:
    if platform.machine().lower() not in {"amd64", "x86_64"}:
        return None
    return {"win32": "windows-x86_64", "linux": "linux-x86_64"}.get(sys.platform)


def download_manifest() -> dict:
    return json.loads(Path(__file__).with_name("cuda-downloads.json").read_text(encoding="utf-8"))


def python_abi() -> str:
    return f"cp{sys.version_info.major}{sys.version_info.minor}" + ("t" if "t" in getattr(sys, "abiflags", "") else "")


def download_plan(manifest: dict | None = None) -> dict:
    manifest = manifest or download_manifest()
    try:
        plan = manifest["platforms"][platform_key()]
        runtime = plan["onnxruntime_wheels"][python_abi()]
    except KeyError as exc:
        raise RuntimeError("No optional CUDA download is available for this platform and Python version.") from exc
    return {**plan, "wheels": [*plan["wheels"], runtime]}


def runtime_directory() -> Path:
    # LocalAppData is writable in MSIX and does not roam several GB of libraries.
    from PySide6.QtCore import QStandardPaths
    from shared.paths import user_home

    location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericDataLocation)
    root = (Path(location) if location else user_home() / ".local/share") / "EIDORA" / "EIDORA Desktop"
    return root / "runtimes" / download_manifest()["id"] / f"{platform_key()}-{python_abi()}"


def _manifest_digest(manifest: dict) -> str:
    return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()


def installed_runtime(directory: Path | None = None, *, manifest: dict | None = None,
                      verify=False, cancelled=lambda: False) -> Path | None:
    """Check the completed receipt and file sizes locally; never contact the network."""
    if platform_key() is None:
        return None
    manifest = manifest or download_manifest()
    directory = directory or runtime_directory()
    try:
        receipt = json.loads((directory / "installed.json").read_text(encoding="utf-8"))
        if (receipt["manifest_sha256"] != _manifest_digest(manifest)
                or receipt["platform"] != platform_key() or receipt["python_abi"] != python_abi()):
            return None
        files = receipt["files"]
        if not files:
            return None
        for relative, details in files.items():
            path = PurePosixPath(relative)
            if path.is_absolute() or ".." in path.parts or "\\" in relative or ":" in relative:
                return None
            target = directory / relative
            if target.is_symlink() or not target.is_file() or target.stat().st_size != details["size"]:
                return None
            if verify:
                digest = hashlib.sha256()
                with target.open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        _cancel(cancelled)
                        digest.update(chunk)
                if digest.hexdigest() != details["sha256"]:
                    return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return directory


def runtime_available() -> bool:
    if installed_runtime() is not None:
        return True
    if getattr(sys, "frozen", False) or platform_key() is None:
        return False
    # Preserve manually provisioned source environments using the GPU package.
    from importlib.metadata import PackageNotFoundError, distribution

    try:
        if distribution("onnxruntime-gpu").version != download_manifest()["onnxruntime"]:
            return False
        for wheel in download_manifest()["platforms"][platform_key()]["wheels"]:
            if distribution(wheel["package"]).version != wheel["version"]:
                return False
    except PackageNotFoundError:
        return False
    return True


def _cancel(cancelled) -> None:
    if cancelled():
        raise InstallCancelled("CUDA download cancelled.")


def _download(wheel: dict, target: Path, cancelled, progress) -> None:
    import certifi

    url = wheel["url"]
    if urlparse(url).scheme != "https" or urlparse(url).hostname != "files.pythonhosted.org":
        raise ValueError("CUDA downloads must use the pinned PyPI file host.")
    digest = hashlib.sha256()
    received = 0
    context = ssl.create_default_context(cafile=certifi.where())
    with urlopen(url, timeout=15, context=context) as response, target.open("wb") as output:
        if urlparse(response.geturl()).scheme != "https" or urlparse(response.geturl()).hostname != "files.pythonhosted.org":
            raise ValueError("Unexpected CUDA download redirect.")
        while True:
            _cancel(cancelled)
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            received += len(chunk)
            if received > wheel["size"]:
                raise ValueError("CUDA download exceeds its pinned size.")
            digest.update(chunk)
            output.write(chunk)
            progress(received)
    if received != wheel["size"] or digest.hexdigest() != wheel["sha256"]:
        raise ValueError(f"CUDA download verification failed: {wheel['package']}.")


def _extract(wheel: dict, archive: Path, staging: Path, files: dict, budget: int, cancelled) -> int:
    libraries = 0
    with zipfile.ZipFile(archive) as source:
        for entry in source.infolist():
            _cancel(cancelled)
            path = PurePosixPath(entry.filename)
            if path.is_absolute() or ".." in path.parts or "\\" in entry.filename or ":" in entry.filename or stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError("Unsafe CUDA archive entry.")
            if entry.is_dir():
                continue
            runtime_file = wheel["package"] == "onnxruntime-gpu" and path.parts[0] == "onnxruntime"
            library = path.name.endswith(".dll") or ".so" in path.name or path.suffix == ".pyd"
            license_file = "license" in path.name.lower() or "notice" in path.name.lower()
            if not library and not license_file and not runtime_file:
                continue
            if wheel["package"] == "onnxruntime-gpu" and library and not runtime_file:
                raise ValueError("Unexpected native library outside the ONNX Runtime package.")
            relative = (f"python/{path.as_posix()}" if runtime_file else path.name if library else
                        f"licenses/{wheel['package']}/{path.name}")
            if relative in files:
                raise ValueError(f"Duplicate CUDA archive filename: {relative}")
            budget -= entry.file_size
            if budget < 0:
                raise ValueError("CUDA runtime exceeds its extraction limit.")
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            with source.open(entry) as input_file, target.open("wb") as output:
                while chunk := input_file.read(1024 * 1024):
                    _cancel(cancelled)
                    output.write(chunk)
                    digest.update(chunk)
            files[relative] = {"size": entry.file_size, "sha256": digest.hexdigest()}
            libraries += library
    if not libraries:
        raise ValueError(f"No runtime libraries in {wheel['package']}.")
    return budget


def install_runtime(*, directory: Path | None = None, manifest: dict | None = None,
                    cancelled=lambda: False, progress=lambda percent, message: None) -> Path:
    """Verify each archive, retain licenses, then atomically publish a complete runtime."""
    manifest = manifest or download_manifest()
    key = platform_key()
    if key not in manifest["platforms"]:
        raise RuntimeError("Optional CUDA is supported on Windows and Linux x64 only.")
    directory = directory or runtime_directory()
    directory.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(directory) + ".lock", timeout=0)
    try:
        with lock:
            progress(0, "Checking existing NVIDIA GPU support…")
            if installed_runtime(directory, manifest=manifest, verify=True, cancelled=cancelled):
                return directory
            plan = download_plan(manifest)
            total = sum(w["size"] for w in plan["wheels"])
            required = plan["installed_size_limit"] + max(w["size"] for w in plan["wheels"]) + 128 * 1024**2
            if shutil.disk_usage(directory.parent).free < required:
                raise RuntimeError(f"CUDA setup needs at least {required / 1024**3:.1f} GiB of free disk space.")
            # One wheel at a time bounds temporary disk use. Failed downloads are
            # discarded; neither a failed attempt nor cancellation can become active.
            with tempfile.TemporaryDirectory(prefix=".cuda-install-", dir=directory.parent) as temporary:
                staging = Path(temporary) / "runtime"
                staging.mkdir()
                files = {}
                completed = 0
                budget = plan["installed_size_limit"]
                for wheel in plan["wheels"]:
                    _cancel(cancelled)
                    archive = Path(temporary) / "download.whl"
                    _download(wheel, archive, cancelled,
                              lambda received: progress(min(94, int(94 * (completed + received) / total)), "Downloading NVIDIA GPU support…"))
                    progress(min(95, int(94 * (completed + wheel["size"]) / total)), "Verifying and unpacking NVIDIA GPU support…")
                    budget = _extract(wheel, archive, staging, files, budget, cancelled)
                    archive.unlink()
                    completed += wheel["size"]
                _cancel(cancelled)
                receipt = {"manifest_sha256": _manifest_digest(manifest), "platform": key,
                           "python_abi": python_abi(), "files": files}
                (staging / "installed.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
                # A damaged same-version install is preserved until the replacement
                # is complete. Windows refuses replacement if its DLLs are in use.
                previous = Path(temporary) / "previous"
                if directory.exists():
                    directory.rename(previous)
                try:
                    staging.rename(directory)
                except BaseException:
                    if previous.exists():
                        previous.rename(directory)
                    raise
            progress(96, "NVIDIA GPU support installed. Checking the device…")
            return directory
    except Timeout as exc:
        raise RuntimeError("CUDA setup is already running in another EIDORA window.") from exc


def enable_runtime(*, cancelled=lambda: False, progress=lambda percent, message: None) -> None:
    """Check real CUDA inference in a disposable process after installing libraries."""
    import subprocess
    import time
    from shared.internal_workers import internal_worker_command, read_worker_result

    install_runtime(cancelled=cancelled, progress=progress)
    progress(97, "Checking NVIDIA GPU inference…")
    with tempfile.TemporaryDirectory(prefix="eidora-cuda-check-") as temporary:
        root = Path(temporary)
        result = root / "result.json"
        with (root / "worker.log").open("w", encoding="utf-8") as log:
            child = subprocess.Popen(internal_worker_command("onnx-audit", root, result, "--provider", "cuda"),
                                     stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 120
                while child.poll() is None:
                    _cancel(cancelled)
                    if time.monotonic() >= deadline:
                        raise RuntimeError("CUDA device check timed out. GPU support is downloaded; retry after checking the NVIDIA driver.")
                    time.sleep(0.1)
                _cancel(cancelled)
                try:
                    reply = read_worker_result(result)
                except RuntimeError as exc:
                    detail = (root / "worker.log").read_text(encoding="utf-8", errors="replace")[-2000:]
                    raise RuntimeError(
                        f"GPU support is downloaded, but the device check exited without a result "
                        f"(code {child.returncode}). Check the NVIDIA driver and retry. {detail}"
                    ) from exc
                if child.returncode or not reply["ok"]:
                    raise RuntimeError("GPU support is downloaded, but the CUDA check failed. "
                                       "Update the NVIDIA driver (Windows 570.65+ / Linux 570.26+) and retry. "
                                       + str(reply.get("error", "Worker failed."))[-2000:])
            finally:
                if child.poll() is None:
                    child.kill()
                child.wait()
    progress(100, "NVIDIA GPU support is ready.")
