from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class Job:
    id: str
    panel: str
    projection: bool = False


class Scheduler:
    """Fair panel-serialized admission with reserved projection capacity."""

    def __init__(self, *, general_limit: int = 2, projection_limit: int = 1) -> None:
        self.general_limit = max(1, int(general_limit))
        self.projection_limit = max(1, int(projection_limit))
        self.running: dict[str, Job] = {}
        self._queues: dict[str, deque[Job]] = {}
        self._panel_order: deque[str] = deque()

    def submit(self, job: Job) -> None:
        if job.id in self.running or any(job.id == queued.id for queue in self._queues.values() for queued in queue):
            raise ValueError(f"Background job ID already exists: {job.id}")
        queue = self._queues.get(job.panel)
        if queue is None:
            queue = deque()
            self._queues[job.panel] = queue
            self._panel_order.append(job.panel)
        queue.append(job)

    def admit(self) -> list[Job]:
        admitted: list[Job] = []
        general_running = sum(not job.projection for job in self.running.values())
        projection_running = sum(job.projection for job in self.running.values())
        active_panels = {job.panel for job in self.running.values()}
        for projection, capacity in (
            (True, self.projection_limit - projection_running),
            (False, self.general_limit - general_running),
        ):
            while capacity > 0:
                panel = next(
                    (
                        candidate
                        for candidate in self._panel_order
                        if candidate not in active_panels
                        and self._queues.get(candidate)
                        and self._queues[candidate][0].projection == projection
                    ),
                    None,
                )
                if panel is None:
                    break
                queue = self._queues[panel]
                job = queue.popleft()
                self._panel_order.remove(panel)
                if queue:
                    self._panel_order.append(panel)
                else:
                    self._queues.pop(panel, None)
                self.running[job.id] = job
                active_panels.add(panel)
                admitted.append(job)
                capacity -= 1
        return admitted

    def finish(self, job_id: str) -> bool:
        return self.running.pop(str(job_id), None) is not None

    def cancel(self, job_id: str) -> bool:
        key = str(job_id)
        if key in self.running:
            return False
        for panel, queue in tuple(self._queues.items()):
            for job in tuple(queue):
                if job.id != key:
                    continue
                queue.remove(job)
                if not queue:
                    self._queues.pop(panel, None)
                    try:
                        self._panel_order.remove(panel)
                    except ValueError:
                        pass
                return True
        return False
