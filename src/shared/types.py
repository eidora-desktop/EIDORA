from __future__ import annotations

from typing import Protocol


class SupportsShutdown(Protocol):
    def shutdown(self) -> None:
        """Release resources owned by the object."""

