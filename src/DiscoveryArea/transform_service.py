from __future__ import annotations

from collections.abc import Callable

import numpy as np

from DiscoveryArea.arrangement_repository import ArrangementRepository
from DiscoveryArea.domain import DiscoveryDataset
from DiscoveryArea.transforms import TransformResult, TransformSettings, source_fingerprint, transform_coordinates


class DiscoveryTransformService:
    def repository(self, project) -> ArrangementRepository:
        return ArrangementRepository(project.paths.discovery)

    def transform_and_save(
        self,
        *,
        project,
        discovery_scope: str,
        dataset: DiscoveryDataset,
        settings: TransformSettings,
        cached_grid: np.ndarray | None = None,
        cancelled: Callable[[], bool] | None = None,
        progress: Callable[[int], None] | None = None,
    ) -> TransformResult:
        result = transform_coordinates(
            dataset.coordinates,
            dataset.metadata_ids,
            settings,
            cached_grid=cached_grid,
            cancelled=cancelled,
            progress=progress,
        )
        if cancelled is not None and cancelled():
            from DiscoveryArea.transforms import TransformCancelled

            raise TransformCancelled
        return self.repository(project).save(discovery_scope, result)

    def load_current(self, project, discovery_scope: str, source: DiscoveryDataset) -> TransformResult | None:
        fingerprint = source_fingerprint(source.coordinates, source.metadata_ids)
        result = self.repository(project).load_current(discovery_scope, fingerprint)
        return self._aligned_result(result, source)

    def load_reference(
        self,
        project,
        arrangement_ref: str | None,
        source: DiscoveryDataset,
    ) -> TransformResult | None:
        repository = self.repository(project)
        path = repository.resolve_ref(project.root, arrangement_ref)
        result = repository.load_artifact(path) if path is not None else None
        if result is None or result.source_fingerprint != source_fingerprint(source.coordinates, source.metadata_ids):
            return None
        return self._aligned_result(result, source)

    def relative_ref(self, project, result: TransformResult | None) -> str | None:
        return self.repository(project).relative_ref(project.root, result)

    @staticmethod
    def dataset_for_result(result: TransformResult) -> DiscoveryDataset:
        return DiscoveryDataset(coordinates=result.coordinates.copy(), metadata_ids=result.metadata_ids.copy())

    @staticmethod
    def _aligned_result(result: TransformResult | None, source: DiscoveryDataset) -> TransformResult | None:
        if result is None:
            return None
        if np.array_equal(result.metadata_ids, source.metadata_ids):
            return result
        index = {int(value): row for row, value in enumerate(result.metadata_ids.tolist())}
        try:
            rows = np.asarray([index[int(value)] for value in source.metadata_ids.tolist()], dtype=np.int64)
        except KeyError:
            return None
        from dataclasses import replace

        return replace(
            result,
            metadata_ids=source.metadata_ids.copy(),
            grid_coordinates=result.grid_coordinates[rows].copy(),
            coordinates=result.coordinates[rows].copy(),
        )
