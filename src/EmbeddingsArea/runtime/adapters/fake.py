from __future__ import annotations

import hashlib

import numpy as np

from EmbeddingsArea.runtime.adapters.base import AdapterRunConfig, RuntimeAdapterDescriptor
from EmbeddingsArea.runtime.contracts import RuntimeInput


class DeterministicFakeAdapter:
    descriptor = RuntimeAdapterDescriptor(
        adapter_id="fake",
        backend="fake",
        supported_inputs=("image", "text"),
        primary_input="image",
        aliases=("deterministic",),
    )

    def setup(self, config: AdapterRunConfig) -> None:  # noqa: ARG002
        return

    def embed_batch(self, batch: list[RuntimeInput]) -> np.ndarray:
        return np.asarray([_fake_embedding(item) for item in batch], dtype=np.float32)

    def close(self) -> None:
        return


def _fake_embedding(item: RuntimeInput) -> list[float]:
    digest = hashlib.sha256(f"{item.media_item_id}|{item.identifier}".encode("utf-8")).digest()
    return [
        int.from_bytes(digest[offset : offset + 4], "big") / 2**32
        for offset in (0, 4, 8, 12)
    ]
