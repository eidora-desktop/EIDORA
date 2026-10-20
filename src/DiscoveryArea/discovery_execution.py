from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from typing import TYPE_CHECKING

import numpy as np
from PySide6 import QtCore

from DiscoveryArea.method_catalog import DiscoveryMethodCatalog
from DiscoveryArea.service import DiscoveryService
from shared.internal_workers import internal_worker_command

if TYPE_CHECKING:
    from project.domain import Project


class DiscoveryRunBridge(QtCore.QObject):
    finished = QtCore.Signal(str)
    failed = QtCore.Signal(str)
    cancelled = QtCore.Signal()
    progress = QtCore.Signal(int, str)
    thread_finished = QtCore.Signal()

    @QtCore.Slot(str)
    def on_finished(self, option_key: str) -> None:
        self.finished.emit(option_key)

    @QtCore.Slot(str)
    def on_failed(self, message: str) -> None:
        self.failed.emit(message)

    @QtCore.Slot()
    def on_cancelled(self) -> None:
        self.cancelled.emit()

    @QtCore.Slot(int, str)
    def on_progress(self, value: int, message: str) -> None:
        self.progress.emit(value, message)

    @QtCore.Slot()
    def on_thread_finished(self) -> None:
        self.thread_finished.emit()


class DiscoveryRunWorker(QtCore.QObject):
    finished = QtCore.Signal(str)
    failed = QtCore.Signal(str)
    cancelled = QtCore.Signal()
    progress = QtCore.Signal(int, str)

    def __init__(
        self,
        project: "Project",
        payload: dict[str, object],
        metadata_ids: np.ndarray,
        artifact_path: Path | None,
        method_catalog: DiscoveryMethodCatalog | None = None,
    ) -> None:
        super().__init__()
        self._project = project
        self._payload = dict(payload)
        self._metadata_ids = np.asarray(metadata_ids, dtype=np.int64)
        self._artifact_path = artifact_path
        self._method_catalog = method_catalog or DiscoveryMethodCatalog()
        self._progress_value = 0
        self._current_activity = "Preparing projection request..."
        self._last_progress_update: tuple[int, str] | None = None
        self._cancel_requested = threading.Event()
        self._process_lock = threading.Lock()
        self._active_process: subprocess.Popen[str] | None = None
        self._output_slug = self._planned_output_slug()

    def request_cancel(self) -> None:
        """Request cancellation from the GUI thread without blocking it."""
        self._cancel_requested.set()
        with self._process_lock:
            process = self._active_process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def run(self) -> None:
        option_key: str | None = None
        try:
            self._raise_if_cancelled()
            method_id = str(self._payload.get("method") or "random").strip().lower()
            self._emit_progress(2, "Preparing projection request...")
            definition = self._method_catalog.get(method_id)
            if definition is not None and definition.execution == "subprocess":
                option_key = self._run_isolated_discovery_process(method_id)
                self._raise_if_cancelled()
                self._emit_progress(94, f"Saved {definition.name} projection. Preparing display...")
                self._raise_if_cancelled()
                self.finished.emit(option_key)
                return
            option = run_discovery_request(
                self._project,
                self._payload,
                self._metadata_ids,
                self._artifact_path,
                progress_callback=self._on_service_progress,
                status_callback=self._on_service_status,
            )
            option_key = option.key
            self._raise_if_cancelled()
            self._emit_progress(94, f"Saved projection: {option.label}. Preparing display...")
            self._raise_if_cancelled()
            self.finished.emit(option.key)
        except DiscoveryRunCancelled:
            if option_key:
                DiscoveryService().discard_discovery_result(self._project, option_key)
            self._cleanup_planned_output()
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(str(exc))

    def _run_isolated_discovery_process(self, method_id: str) -> str:
        with tempfile.TemporaryDirectory(prefix=f"eidora_discovery_{method_id}_") as tmp_dir_raw:
            self._emit_progress(5, "Preparing isolated projection process...")
            tmp_dir = Path(tmp_dir_raw)
            request_path = tmp_dir / "request.json"
            metadata_ids_path = tmp_dir / "metadata_ids.npy"
            result_path = tmp_dir / "result.json"
            request_path.write_text(
                json.dumps(
                    {
                        "project_name": self._project.name,
                        "project_root": str(self._project.root),
                        "project_schema_version": int(self._project.schema_version),
                        "artifact_path": str(self._artifact_path) if self._artifact_path is not None else "",
                        "payload": self._payload,
                    }
                ),
                encoding="utf-8",
            )
            np.save(metadata_ids_path, self._metadata_ids)
            env = _projection_process_environment()
            self._emit_progress(35, f"Running {method_id} in isolated process...")
            process = subprocess.Popen(
                internal_worker_command(
                    "discovery",
                    str(request_path),
                    str(metadata_ids_path),
                    str(result_path),
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            with self._process_lock:
                self._active_process = process
            try:
                while True:
                    try:
                        stdout, stderr = process.communicate(timeout=0.1)
                        break
                    except subprocess.TimeoutExpired:
                        self._raise_if_cancelled()
                self._raise_if_cancelled()
            finally:
                with self._process_lock:
                    self._active_process = None
                if process.poll() is None:
                    try:
                        process.terminate()
                    except OSError:
                        pass
                    try:
                        process.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        try:
                            process.kill()
                        except OSError:
                            pass
                        process.wait(timeout=2.0)
            if result_path.exists():
                self._emit_progress(92, "Reading saved projection result...")
                payload = json.loads(result_path.read_text(encoding="utf-8"))
                if payload.get("ok") is True:
                    return str(payload.get("option_key") or "")
                error = str(payload.get("error") or "").strip()
                if error:
                    raise RuntimeError(error)
            signal_hint = ""
            if process.returncode is not None and process.returncode < 0:
                signal_hint = f" (terminated by signal {-process.returncode})"
            detail = (stderr or stdout or "").strip()
            raise RuntimeError(f"Discovery worker failed{signal_hint}: {detail or 'no diagnostic output'}")

    def _on_service_progress(self, value: int) -> None:
        self._raise_if_cancelled()
        normalized = max(0, min(92, int(value)))
        self._emit_progress(normalized, self._current_activity)

    def _on_service_status(self, message: str) -> None:
        self._raise_if_cancelled()
        self._current_activity = str(message)
        self._emit_progress(self._progress_value, self._current_activity)

    def _emit_progress(self, value: int, message: str) -> None:
        normalized = max(self._progress_value, min(94, max(0, int(value))))
        self._progress_value = normalized
        update = (normalized, str(message))
        if update == self._last_progress_update:
            return
        self._last_progress_update = update
        self.progress.emit(*update)

    def _raise_if_cancelled(self) -> None:
        if self._cancel_requested.is_set():
            raise DiscoveryRunCancelled

    def _cleanup_planned_output(self) -> None:
        slug = self._output_slug
        for directory, suffix in (
            (self._project.paths.discovery, ".sqlite"),
            (self._project.paths.discovery_presets, ".json"),
        ):
            destination = directory / f"{slug}{suffix}"
            destination.unlink(missing_ok=True)
            for temporary in directory.glob(f".{slug}.*.tmp"):
                temporary.unlink(missing_ok=True)

    def _planned_output_slug(self) -> str:
        label = str(self._payload.get("name") or "Discovery").strip() or "Discovery"
        base_slug = "_".join(
            part
            for part in "".join(character.lower() if character.isalnum() else "_" for character in label).split("_")
            if part
        ) or "discovery"
        presets_dir = self._project.paths.discovery_presets
        slug = base_slug
        index = 2
        while (presets_dir / f"{slug}.json").exists() or (
            self._project.paths.discovery / f"{slug}.sqlite"
        ).exists():
            slug = f"{base_slug}_{index}"
            index += 1
        return slug


class DiscoveryRunCancelled(Exception):
    """Internal cooperative-cancellation signal for a projection run."""


def _projection_process_environment() -> dict[str, str]:
    """Return an isolated-process environment with bounded CPU parallelism."""
    env = dict(os.environ)
    available = max(1, int(os.cpu_count() or 1))
    raw_thread_count = str(env.get("EIDORA_PROJECTION_THREADS") or "").strip()
    try:
        requested = int(raw_thread_count) if raw_thread_count else min(available, 8)
    except ValueError:
        requested = min(available, 8)
    thread_count = max(1, min(requested, available))
    for name in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "NUMBA_NUM_THREADS",
        "LOKY_MAX_CPU_COUNT",
    ):
        env[name] = str(thread_count)
    return env


def run_discovery_request(
    project: "Project",
    payload: dict[str, object],
    metadata_ids: np.ndarray,
    artifact_path: Path | None,
    *,
    progress_callback=None,
    status_callback=None,
):
    service = DiscoveryService()
    method_id = str(payload.get("method") or "random").strip().lower()
    label = str(payload.get("name") or "Discovery").strip() or "Discovery"
    parameters = payload.get("parameters")
    parameter_values = parameters if isinstance(parameters, dict) else {}
    if method_id == "custom_coordinates":
        coordinate_file = payload.get("coordinate_file")
        if not coordinate_file:
            raise ValueError("Select a coordinate file.")
        return service.create_discovery_from_custom_coordinates(
            project,
            coordinate_file=Path(str(coordinate_file)),
            metadata_ids=np.asarray(metadata_ids, dtype=np.int64),
            label=label,
            progress_callback=progress_callback,
            status_callback=status_callback,
        )
    if method_id != "random" and artifact_path is None:
        raise ValueError("Select a completed embeddings source.")
    return service.create_discovery_from_artifact(
        project,
        artifact_path=artifact_path or project.root,
        metadata_ids=np.asarray(metadata_ids, dtype=np.int64),
        label=label,
        method_id=method_id,
        parameters=parameter_values,
        source_label=str(payload.get("source_label") or ""),
        source_key=str(payload.get("source_key") or ""),
        filter_preset_label=str(payload.get("filter_preset_label") or ""),
        filter_preset_snapshot=(
            dict(payload.get("filter_preset_snapshot"))
            if isinstance(payload.get("filter_preset_snapshot"), dict)
            else None
        ),
        progress_callback=progress_callback,
        status_callback=status_callback,
    )


ProjectionRunBridge = DiscoveryRunBridge
ProjectionRunWorker = DiscoveryRunWorker
run_projection_request = run_discovery_request
