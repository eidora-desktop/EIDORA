"""Background document writes and a single isolated preview/export renderer."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import multiprocessing
from pathlib import Path
import tempfile
import threading

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
    def __init__(self, state_root: Path | None = None, *, cleanup_on_close: bool = False) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="eidora-figures-") if state_root is None else None
        self.repository = FigureTabsRepository(state_root or Path(self._temporary.name) / "state")
        self._renders = FigureRenderScheduler()
        self._writes = ThreadPoolExecutor(max_workers=1, thread_name_prefix="figure-save")
        self._loads = ThreadPoolExecutor(max_workers=1, thread_name_prefix="figure-load")
        self._process = None
        self._connection = None
        self._lock = threading.Lock()
        self._closed = False
        self._preparations = set()
        self._cleanup_on_close = cleanup_on_close

    def save(self, records, current_index):
        return self._writes.submit(self.repository.save, records, current_index)

    def load(self):
        return self._renders.submit(self._request, {"load": str(self.repository.data.root.parent)})

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
        composition.plot_cells[0, 0] = prepared
        element = composition.figure_elements.get('plot_title_settings_0_0')
        if element is not None and element.auto_text:
            element.text = prepared.snapshot_label or 'Plot 1, 1'
        if self._closed or cancelled():
            raise RuntimeError('Figure frame preparation cancelled')
        return self.preview(composition, destination=destination).result()

    def _render(self, composition, destination, incremental=False, base_frame=None, *, may_draw=lambda: True):
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
            result = self._request(request)
            result["datasets"] = datasets
            return result
        # The parent owns staging so terminating the renderer cannot leave
        # partial exports behind or overwrite an existing destination.
        with tempfile.TemporaryDirectory(prefix=".eidora-export-", dir=destination.parent) as directory:
            staged = Path(directory) / destination.name
            self._request({**request, "export": str(staged)})
            staged.replace(destination)
        return {"export": str(destination)}

    def _request(self, request):
        with self._lock:
            if self._closed:
                raise RuntimeError("Figure renderer has closed")
            if self._process is None or not self._process.is_alive():
                if self._connection is not None:
                    self._connection.close()
                from FiguresArea.preview_worker import run
                context = multiprocessing.get_context("spawn")
                connection, child = context.Pipe()
                process = context.Process(target=run, args=(child,), daemon=True, name="figure-renderer")
                process.start()
                child.close()
                self._connection, self._process = connection, process
            connection = self._connection
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

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for event in self._preparations.copy():
                event.set()
            if self._process is not None:
                self._process.terminate()
                self._process.join(timeout=2)
            if self._connection is not None:
                self._connection.close()
        self._renders.shutdown(wait=True, cancel_futures=True)
        self._loads.shutdown(wait=True, cancel_futures=True)
        # Saving is transactional and must finish even when a preview is cancelled.
        self._writes.shutdown(wait=True)
        try:
            if self._cleanup_on_close:
                self.repository.remove_unused_data()
        except (OSError, ValueError, TypeError):
            # Cleanup is optional; never interfere with durable document writes.
            pass
        if self._temporary is not None:
            self._temporary.cleanup()
