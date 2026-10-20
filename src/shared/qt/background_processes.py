from __future__ import annotations

from dataclasses import dataclass
from itertools import count
from typing import Callable
import weakref

from shared.background.scheduler import Job, Scheduler


@dataclass
class ProcessRecord:
    id: int
    panel: str
    title: str
    projection: bool
    start: Callable[[], None]
    cancel_callback: Callable[[], None] | None
    failed: Callable[[str], None] | None
    state: str = "queued"
    cancelled: bool = False
    start_invoked: bool = False


class ProcessAdmission:
    def __init__(self, manager: "BackgroundProcessManager", record_id: int) -> None:
        self._manager = weakref.ref(manager)
        self._record_id = record_id

    @property
    def queued(self) -> bool:
        manager = self._manager()
        record = manager.record(self._record_id) if manager is not None else None
        return bool(record and record.state == "queued")

    def cancel(self) -> None:
        manager = self._manager()
        if manager is not None:
            manager.cancel(self._record_id)

    def pause(self) -> None:
        manager = self._manager()
        if manager is not None:
            manager.pause(self._record_id)

    def finish(self) -> None:
        manager = self._manager()
        if manager is not None:
            manager.finish(self._record_id)

    def update(self, **_kwargs: object) -> None:
        # Stale worker updates are intentionally ignored after finish.
        return


class BackgroundProcessManager:
    """Fair two-slot scheduler with a separately reserved projection slot."""

    def __init__(self, *, changed: Callable[[], None]) -> None:
        self.records: dict[int, ProcessRecord] = {}
        self.scheduler = Scheduler()
        self._ids = count(1)
        self._changed = changed
        self._publication_active = False

    def begin_metadata_publication(self) -> None:
        """Reserve an idle scheduler until a modal metadata import has finalized."""
        if self._publication_active or self.records:
            raise ValueError("Finish or cancel background processes before importing metadata.")
        self._publication_active = True

    def end_metadata_publication(self) -> None:
        self._publication_active = False
        self.dispatch()

    def submit(
        self,
        *,
        panel: str,
        title: str,
        start: Callable[[], None],
        cancel: Callable[[], None] | None = None,
        failed: Callable[[str], None] | None = None,
        projection: bool = False,
    ) -> ProcessAdmission:
        record_id = next(self._ids)
        self.records[record_id] = ProcessRecord(
            record_id, str(panel), str(title), bool(projection), start, cancel, failed
        )
        self.scheduler.submit(Job(str(record_id), str(panel), bool(projection)))
        self._notify()
        return ProcessAdmission(self, record_id)

    def record(self, record_id: int) -> ProcessRecord | None:
        return self.records.get(record_id)

    def dispatch(self) -> None:
        if self._publication_active:
            return
        while True:
            admitted = self.scheduler.admit()
            if not admitted:
                break
            for job in sorted(admitted, key=lambda value: int(value.id)):
                candidate = self.records.get(int(job.id))
                if candidate is None:
                    self.scheduler.finish(job.id)
                    continue
                candidate.state = "running"
                self._notify()
                if candidate.cancelled or candidate.id not in self.records:
                    self.scheduler.finish(str(candidate.id))
                    continue
                candidate.start_invoked = True
                try:
                    candidate.start()
                except Exception as exc:  # noqa: BLE001
                    self.records.pop(candidate.id, None)
                    self.scheduler.finish(str(candidate.id))
                    if candidate.failed is not None:
                        candidate.failed(str(exc))
                    self._notify()

    def cancel(self, record_id: int) -> None:
        record = self.records.get(record_id)
        if record is None or record.cancelled:
            return
        record.cancelled = True
        if record.cancel_callback is not None:
            record.cancel_callback()
        if record.state in {"queued", "paused"} or (
            record.state == "running" and not record.start_invoked
        ):
            self.scheduler.cancel(str(record_id))
            self.scheduler.finish(str(record_id))
            self.records.pop(record_id, None)
        self._notify()

    def pause(self, record_id: int) -> None:
        record = self.records.get(record_id)
        if record is None or record.state != "running":
            return
        record.state = "paused"
        self.scheduler.finish(str(record_id))
        self._notify()
        self.dispatch()

    def finish(self, record_id: int) -> None:
        self.scheduler.finish(str(record_id))
        if self.records.pop(record_id, None) is not None:
            self._notify()
            self.dispatch()

    def shutdown(self) -> None:
        for record_id in tuple(self.records):
            self.cancel(record_id)
        self.records.clear()
        self._notify()

    def _notify(self) -> None:
        self._changed()
