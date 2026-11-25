"""Background document writes and a single isolated preview/export renderer."""
from __future__ import annotations

from concurrent.futures import CancelledError, ThreadPoolExecutor, TimeoutError
import multiprocessing
from pathlib import Path
import tempfile
import threading
import time

from FiguresArea.repository import FigureTabsRepository
from FiguresArea.render_scheduler import FigureRenderScheduler


class _PreparationFuture:
    def __init__(self, future, event):
        self._future, self._event = future, event

    def cancel(self):
        self._event.set()
        return self._future.cancel()

    def done(self):
        return self._future.done()

    def add_done_callback(self, callback):
        self._future.add_done_callback(callback)

    def result(self, timeout=None):
        return self._future.result(timeout=timeout)


class FigureWorkService:
    def __init__(self, state_root: Path | None = None, *, cleanup_on_close: bool = False,
                 request_timeout: float = 300, predecessor: FigureWorkService | None = None) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="eidora-figures-") if state_root is None else None
        self.repository = FigureTabsRepository(state_root or Path(self._temporary.name) / "state")
        self._renders = FigureRenderScheduler()
        self._writes = ThreadPoolExecutor(max_workers=1, thread_name_prefix="figure-save")
        self._loads = ThreadPoolExecutor(max_workers=1, thread_name_prefix="figure-load")
        self._transport = ThreadPoolExecutor(max_workers=1, thread_name_prefix="figure-transport")
        self._request_timeout = request_timeout
        self._process = None
        self._connection = None
        self._lock = threading.Lock()
        self._closed = False
        self._retirement = None
        self._preparations = set()
        self._cleanup_on_close = cleanup_on_close
        self._predecessor = predecessor

    @property
    def retired(self):
        return self._retirement is not None and not self._retirement.is_alive()

    def _wait_for_previous_service(self):
        previous = self._predecessor
        if previous is not None:
            previous.shutdown()
            self._predecessor = None

    def save(self, records, current_index):
        with self._lock:
            if self._closed:
                raise RuntimeError('Figure renderer has closed')
            return self._writes.submit(self._save, records, current_index)

    def _save(self, records, current_index):
        try:
            self._wait_for_previous_service()
            self.repository.save(records, current_index)
        except Exception as exc:
            from shared.diagnostics.service import record_failure
            record_failure("Saving Figures", exc)
            raise

    def load(self):
        return self._loads.submit(self._load)

    def _load(self):
        self._wait_for_previous_service()
        return self.repository.load()

    def prepare_saved_tab(self, record):
        return self._loads.submit(self.repository.prepare_tab, record)

    def prepare_measurements(self, composition, values):
        from FiguresArea.measurements import measurement_snapshot
        return self._renders.submit(self._request, {
            'measurements': measurement_snapshot(composition),
            'measurement_values': dict(values),
        })

    def set_preview_active(self, owner, active):
        self._renders.set_active(owner, active)

    def cancel_preview(self, owner):
        self._renders.cancel_preview(owner)

    def preview(self, composition, *, destination: Path | None = None, incremental=False, base_frame=None, owner=None):
        if owner is not None and destination is None:
            return self._renders.submit_preview(owner, lambda may_draw: self._render(
                composition, None, incremental, base_frame, may_draw=may_draw))
        return self._renders.submit(self._render, composition, destination, incremental, base_frame)

    def _prepare(self, operation, *args):
        with self._lock:
            if self._closed:
                raise RuntimeError('Figure renderer has closed')
            event = threading.Event()
            self._preparations.add(event)
            future = self._loads.submit(operation, *args, cancelled=event.is_set)
        future.add_done_callback(lambda _: self._preparations.discard(event))
        return _PreparationFuture(future, event)

    def prepare_snapshot(self, loader, path, *, projection=False, overrides=None):
        if overrides is not None:
            return self._prepare(loader.prepare_with_overrides, path, overrides)
        return self._prepare(loader.prepare_projection if projection else loader.prepare, path)

    def export_frame(self, loader, composition, frame, base_path, destination):
        return self._prepare(self._export_frame, loader, composition, frame, base_path, destination)

    def _export_frame(self, loader, composition, frame, base_path, destination, *, cancelled):
        from FiguresArea.domain import SnapshotAnimationFrame
        import copy
        overrides = copy.deepcopy(frame.plot_overrides)
        if isinstance(frame, SnapshotAnimationFrame):
            path = Path(frame.snapshot_path)
        else:
            path = Path(base_path)
            overrides.setdefault('state', {})['slicer_selection'] = {
                'column': frame.column, 'values': list(frame.active_values)}
        prepared = loader.prepare_with_overrides(path, overrides, cancelled=cancelled)
        if not isinstance(frame, SnapshotAnimationFrame):
            prepared.snapshot_label = frame.label
        target = composition.plot_cells.get((0, 0)) or composition.add_plot_cell(0, 0)
        prepared.element_ids = target.element_ids
        prepared.size_overrides = dict(target.size_overrides)
        composition.plot_cells[0, 0] = prepared
        element = composition.figure_elements.get('plot_title_settings_0_0')
        if element is not None and element.auto_text:
            element.text = prepared.snapshot_label or 'Plot 1, 1'
        if self._closed or cancelled():
            raise RuntimeError('Figure frame preparation cancelled')
        return self._renders.submit(lambda: self._render(
            composition, destination, may_draw=lambda: not cancelled()
        )).result()

    def _render(self, composition, destination, incremental=False, base_frame=None, *, may_draw=lambda: True):
        self._wait_for_previous_service()
        payload = self.repository.composition_payload(composition, data=self.repository.data)
        request = {
            "composition": payload, "settings": composition.settings,
            "data_keys": {key: cell.data_key for key, cell in composition.plot_cells.items()},
            "incremental": incremental, "base_frame": base_frame,
        }
        if destination is None:
            datasets = [
                (cell["row"], cell["column"], cell["data_ref"], cell["point_count"],
                 composition.plot_cells[cell["row"], cell["column"]].data_key)
                for cell in payload["plot_cells"] if cell.get("data_ref")
            ]
            # Publishing data remains necessary after a tab becomes hidden:
            # the GUI can adopt these references and release its large arrays.
            # Drawing and image transfer can be skipped before entering Agg.
            if not may_draw():
                return {"datasets": datasets, "publication_only": True}
            try:
                result = self._request(request, cancelled=lambda: not may_draw(), cancellation_grace=.25)
            except CancelledError:
                return {"datasets": datasets, "publication_only": True}
            result["datasets"] = datasets
            return result
        # The parent owns staging so terminating the renderer cannot leave
        # partial exports behind or overwrite an existing destination.
        with tempfile.TemporaryDirectory(prefix=".eidora-export-", dir=destination.parent) as directory:
            staged = Path(directory) / destination.name
            self._request({**request, "export": str(staged)}, cancelled=lambda: not may_draw())
            if self._closed or not may_draw():
                raise CancelledError()
            staged.replace(destination)
        return {"export": str(destination)}

    def _request(self, request, *, cancelled=lambda: False, cancellation_grace=0):
        self._wait_for_previous_service()
        if cancelled():
            raise CancelledError()
        with self._lock:
            if self._closed:
                raise RuntimeError("Figure renderer has closed")
            process, connection = self._process, self._connection
        # Process creation can be slow on Windows. Never hold the GUI-facing
        # lifetime lock while starting a child.
        if process is None or not process.is_alive():
            if connection is not None:
                connection.close()
            self._start_renderer()
        with self._lock:
            if self._closed:
                raise CancelledError()
            connection = self._connection
        # Poll the whole exchange, including sends and partial pixel packets.
        # Connection.poll() alone cannot interrupt recv() on an incomplete frame.
        future = self._transport.submit(self._exchange, connection, request)
        # Full-quality exports can legitimately take longer than an interactive
        # preview. They remain interruptible through cancellation or shutdown.
        deadline = None if 'export' in request else time.monotonic() + self._request_timeout
        cancelled_at = None
        try:
            while True:
                # Let a short obsolete preview finish and retain its warm
                # renderer/cache. A slow one must release the lane for new work.
                now = time.monotonic()
                cancelled_at = (cancelled_at or now) if cancelled() else None
                if self._closed or (cancelled_at is not None and now - cancelled_at >= cancellation_grace):
                    raise CancelledError()
                if deadline is not None and now >= deadline:
                    raise TimeoutError("Figure renderer did not respond in time; retry the operation.")
                try:
                    return future.result(timeout=.05)
                except TimeoutError:
                    if future.done():
                        raise
        except Exception:
            self._stop_renderer()
            raise

    def _start_renderer(self):
        from FiguresArea.preview_worker import run
        context = multiprocessing.get_context('spawn')
        connection, child = context.Pipe()
        process = context.Process(target=run, args=(child,), daemon=True, name='figure-renderer')
        try:
            process.start()
        except Exception:
            connection.close()
            raise
        finally:
            child.close()
        with self._lock:
            self._connection, self._process = connection, process
            closed = self._closed
        if closed:
            self._stop_renderer()

    @staticmethod
    def _exchange(connection, request):
        connection.send(request)
        ok, result = connection.recv()
        if not ok:
            raise RuntimeError(result)
        if "pixel_bytes" in result:
            expected = result.pop('pixel_bytes')
            chunk_size = result.pop('pixel_chunk_bytes')
            regions = result.pop('regions', None)
            if expected < 0 or (expected == 0 and regions != []) or chunk_size <= 0:
                raise RuntimeError('Invalid figure preview image size')
            # Join bounded packets once into immutable bytes. A growing
            # BytesIO/bytearray (including one large recv_bytes call) can
            # retain an extra full-page allocation on each GUI update.
            def receive(length):
                data = b''.join(connection.recv_bytes(min(chunk_size, length - offset))
                                for offset in range(0, length, chunk_size))
                if len(data) != length:
                    raise RuntimeError('Incomplete figure preview image')
                return data
            if regions is None:
                result['rgba'] = receive(expected)
            else:
                width, height = result['size']
                if any(x < 0 or y < 0 or w <= 0 or h <= 0 or x+w > width or y+h > height
                       for x, y, w, h in regions) or sum(w*h*4 for x, y, w, h in regions) != expected:
                    raise RuntimeError('Invalid figure preview regions')
                result['patches'] = [(rect, receive(rect[2]*rect[3]*4)) for rect in regions]
        return result

    def _stop_renderer(self):
        with self._lock:
            process, connection = self._process, self._connection
            self._process = self._connection = None
        if process is not None:
            if process.is_alive():
                process.terminate()
            process.join(timeout=2)
            if process.is_alive():
                process.kill()
                process.join(timeout=2)
        if connection is not None:
            connection.close()

    def shutdown(self, *, wait=True) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                for event in self._preparations.copy():
                    event.set()
                self._retirement = threading.Thread(target=self._finish_shutdown, name="figure-close", daemon=True)
                self._retirement.start()
            retirement = self._retirement
        if wait and retirement is not None:
            retirement.join()

    def _finish_shutdown(self):
        try:
            self._stop_renderer()
        except Exception as exc:
            from shared.diagnostics.service import record_failure
            record_failure('Stopping Figure renderer', exc)
        self._renders.shutdown(wait=True, cancel_futures=True)
        self._transport.shutdown(wait=True, cancel_futures=True)
        self._loads.shutdown(wait=True, cancel_futures=True)
        # Saving is transactional and must finish even when a preview is cancelled.
        self._writes.shutdown(wait=True)
        self._wait_for_previous_service()
        try:
            if self._cleanup_on_close:
                self.repository.remove_unused_data()
        except (OSError, ValueError, TypeError):
            pass
        if self._temporary is not None:
            self._temporary.cleanup()
