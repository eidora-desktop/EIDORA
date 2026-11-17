"""Bound full-resolution images retained by inactive figure views."""
from collections import OrderedDict
import weakref


class PreviewMemoryBudget:
    """Shared LRU for inactive views; active previews keep their full quality."""

    def __init__(self, max_bytes=64 * 1024 * 1024):
        self.max_bytes = max(int(max_bytes), 0)
        self._inactive = OrderedDict()

    def activate(self, preview):
        self._inactive.pop(weakref.ref(preview), None)

    def deactivate(self, preview):
        if weakref.ref(preview) in self._inactive:
            return
        reference = weakref.ref(preview, lambda ref: self._inactive.pop(ref, None))
        self._inactive[reference] = preview.retained_bytes()
        # Keep the already-scaled display where possible, so reactivation can
        # show the last preview while its full-resolution source regenerates.
        for reference in list(self._inactive):
            if sum(self._inactive.values()) <= self.max_bytes:
                break
            candidate = reference()
            if candidate is not None:
                candidate.release_source()
                self._inactive[reference] = candidate.retained_bytes()
        while sum(self._inactive.values()) > self.max_bytes:
            reference, _ = self._inactive.popitem(last=False)
            candidate = reference()
            if candidate is not None:
                candidate.clear()
