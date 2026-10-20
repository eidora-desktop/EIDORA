from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from importlib import metadata as importlib_metadata
import re
from typing import Any

import numpy as np

from EmbeddingsArea.domain import EmbeddingRunState, MediaAsset
from EmbeddingsArea.runtime.readers import load_embedding_matrix_with_ids
from EmbeddingsArea.repository import EmbeddingRunRepository, MediaLinkRepository, MediaRepository
from MetadataArea.repository import MetadataRepository
from MetadataArea.group_repository import MetadataGroupRepository
from MetadataArea.group_service import MetadataGroupService
from DiscoveryArea.domain import (
    PinnedAnnotation,
    PolygonArea,
    PolygonAreaMetadataWritePreview,
    PolygonAreaMetadataWriteResult,
    DiscoveryDataset,
    DiscoveryOption,
)
from DiscoveryArea.filters import (
    FilterMatching,
    FilterRule,
    build_mask,
    filters_from_state_payload,
    filter_matching_from_state_payload,
    required_filter_columns,
)
from DiscoveryArea.metadata_access import DiscoveryMetadataAccess
from DiscoveryArea.method_catalog import DiscoveryMethodCatalog
from DiscoveryArea.discovery_methods.base import ProjectionExecutionResult
from DiscoveryArea.provenance import (
    PROVENANCE_SCHEMA_VERSION,
    ProjectionProvenanceReportService,
    checksum_payload,
    eidora_citation_payload,
)
from eidora_version import __version__ as EIDORA_VERSION
from DiscoveryArea.nearest_neighbors import (
    EmbeddingNeighborIndex,
    NeighborEmbeddingSource,
    build_neighbor_index,
)
from DiscoveryArea.discovery_importer import import_discovery_coordinates
from DiscoveryArea.presets_repository import PresetEntry, PresetRepository
from DiscoveryArea.snapshot_repository import SnapshotRepository
from DiscoveryArea.snapshots import DiscoverySnapshotProjection
from DiscoveryArea.repository import (
    PolygonAreaRepository,
    DiscoveryPinFileRepository,
    DiscoveryRepository,
)
from DiscoveryArea.snapshots import DiscoverySnapshot
from DiscoveryArea.thumbnails.repository import ThumbnailVariant, ThumbnailVariantRepository

if TYPE_CHECKING:
    from project.domain import Project


class DiscoveryService:
    """Application behavior for discovery creation and exploration."""

    def __init__(
        self,
        repository: DiscoveryRepository | None = None,
        method_catalog: DiscoveryMethodCatalog | None = None,
    ) -> None:
        self._repository = repository or DiscoveryRepository()
        self._method_catalog = method_catalog or DiscoveryMethodCatalog()

    def discovery_scope_for_option(self, option: DiscoveryOption) -> str:
        if option.is_random:
            return "random"
        if option.discovery_file is not None:
            return str(option.discovery_file.resolve())
        return str(option.key)

    def polygon_area_repository_for_project(self, project: "Project") -> PolygonAreaRepository:
        return PolygonAreaRepository(project.paths.state / "discovery_areas.db")

    def metadata_group_service_for_project(self, project: "Project") -> MetadataGroupService:
        return MetadataGroupService(
            MetadataGroupRepository(project.paths.metadata_groups_database),
            MetadataRepository(project.paths.metadata_database, project.paths.metadata_columns),
        )

    def linked_media_asset(
        self,
        project: "Project",
        metadata_id: int,
    ) -> MediaAsset | None:
        """Return the authoritative linked source asset for one metadata row."""

        assets = MediaRepository(
            project.paths.media / "media_index.db"
        ).linked_assets_for_metadata_ids([int(metadata_id)])
        return assets.get(int(metadata_id))

    def sync_polygon_area_group(
        self,
        *,
        project: "Project",
        area: PolygonArea,
        discovery_label: str,
        rename_existing: bool = False,
    ) -> tuple[str, str, int]:
        """Mirror one persisted polygon area into its feature-owned custom group."""

        groups = self.metadata_group_service_for_project(project)
        group_set = groups.ensure_producer_group_set(
            f"Areas: {str(discovery_label).strip() or 'Discovery'}",
            "overlapping",
            f"polygon-area-set:{area.discovery_scope}",
        )
        group = groups.ensure_producer_group(
            group_set.group_set_id,
            area.label,
            f"polygon-area:{area.area_id}",
            rename_existing=rename_existing,
        )
        count = groups.save_memberships(group.group_id, area.metadata_ids, "replace")
        return group_set.name, group.name, count

    def remove_polygon_area_group(self, *, project: "Project", area_id: str) -> None:
        self.metadata_group_service_for_project(project).delete_producer_group(
            f"polygon-area:{area_id}"
        )

    def save_neighbor_snapshot_group(
        self,
        *,
        project: "Project",
        group_name: str,
        member_metadata_ids: tuple[int, ...],
    ) -> tuple[str, str, int]:
        """Create one NN snapshot in the feature-owned overlapping set."""

        groups = self.metadata_group_service_for_project(project)
        group_set = groups.ensure_producer_group_set(
            "Nearest Neighbors",
            "overlapping",
            "nearest-neighbor-set",
        )
        group = groups.create_group(group_set.group_set_id, group_name)
        count = groups.save_memberships(group.group_id, member_metadata_ids, "replace")
        return group_set.name, group.name, count

    def save_polygon_areas_as_groups(
        self,
        *,
        project: "Project",
        areas: list[PolygonArea],
        group_set_id: str | None,
        new_group_set_name: str,
        mode: str,
        priority_area_ids: list[str],
    ) -> tuple[str, int, int]:
        groups = self.metadata_group_service_for_project(project)
        if not group_set_id:
            group_set = groups.create_group_set(new_group_set_name, mode)
            group_set_id = group_set.group_set_id
        else:
            group_set = next(
                (item for item in groups.list_group_sets() if item.group_set_id == group_set_id),
                None,
            )
            if group_set is None:
                raise ValueError("The selected group set no longer exists.")
        existing = {item.name.casefold(): item for item in groups.list_groups(group_set_id)}
        group_by_area_id = {}
        for area in areas:
            group = existing.get(area.label.casefold())
            if group is None:
                group = groups.create_group(group_set_id, area.label)
                existing[area.label.casefold()] = group
            group_by_area_id[area.area_id] = group
        area_by_id = {area.area_id: area for area in areas}
        ordered = [area_by_id[value] for value in priority_area_ids if value in area_by_id]
        present = {area.area_id for area in ordered}
        ordered.extend(area for area in areas if area.area_id not in present)
        if group_set.mode.value == "exclusive":
            ordered = list(reversed(ordered))
        for area in ordered:
            groups.save_memberships(
                group_by_area_id[area.area_id].group_id,
                area.metadata_ids,
                "replace",
            )
        affected = len({int(value) for area in areas for value in area.metadata_ids})
        return group_set.name, len(areas), affected

    def pin_repository_for_project(self, project: "Project") -> DiscoveryPinFileRepository:
        return DiscoveryPinFileRepository(project.paths.annotations / "discovery_pins")

    def normalize_polygon_points(self, polygon_raw: object) -> list[list[float]]:
        points: list[list[float]] = []
        if not isinstance(polygon_raw, (list, tuple)):
            return points
        for item in polygon_raw:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            try:
                points.append([float(item[0]), float(item[1])])
            except (TypeError, ValueError):
                continue
        return points

    def metadata_ids_in_polygon(
        self,
        *,
        polygon_points: list[list[float]],
        dataset: DiscoveryDataset,
        visibility: np.ndarray | None,
        visible_threshold: float = 0.5,
    ) -> list[int]:
        polygon = np.asarray(polygon_points, dtype=np.float32)
        if polygon.ndim != 2 or polygon.shape[0] < 3 or dataset.coordinates.shape[0] == 0:
            return []
        if dataset.coordinates.shape[0] != dataset.metadata_ids.shape[0]:
            return []
        from DiscoveryArea.canvas.indexing import points_in_polygon

        inside = points_in_polygon(dataset.coordinates[:, :2].astype(np.float32), polygon)
        if inside.shape[0] != dataset.metadata_ids.shape[0]:
            return []
        if visibility is not None and visibility.shape[0] == inside.shape[0]:
            inside = inside & (visibility >= float(visible_threshold))
        return [int(value) for value in dataset.metadata_ids[inside].tolist()]

    def list_polygon_areas(self, project: "Project", discovery_scope: str) -> list[PolygonArea]:
        return self.polygon_area_repository_for_project(project).list_areas(discovery_scope)

    def save_polygon_area(
        self,
        *,
        project: "Project",
        discovery_scope: str,
        label: str,
        color_hex: str,
        polygon_points: list[list[float]],
        metadata_ids: list[int],
        arrangement_ref: str = "original",
    ) -> PolygonArea:
        return self.polygon_area_repository_for_project(project).upsert_area(
            discovery_scope=discovery_scope,
            label=label,
            color_hex=color_hex,
            polygon_points=polygon_points,
            metadata_ids=metadata_ids,
            arrangement_ref=arrangement_ref,
        )

    def update_polygon_area_geometry(
        self,
        *,
        project: "Project",
        area_id: str,
        polygon_points: list[list[float]],
        metadata_ids: list[int],
    ) -> None:
        self.polygon_area_repository_for_project(project).update_polygon(
            area_id=area_id,
            polygon_points=polygon_points,
            metadata_ids=metadata_ids,
        )

    def remove_polygon_area(self, *, project: "Project", area_id: str) -> None:
        self.polygon_area_repository_for_project(project).remove_area(area_id=area_id)

    def set_polygon_area_visibility(
        self,
        *,
        project: "Project",
        area_id: str,
        area_visible: bool | None = None,
        points_visible: bool | None = None,
    ) -> None:
        self.polygon_area_repository_for_project(project).set_area_visibility(
            area_id=area_id,
            area_visible=area_visible,
            points_visible=points_visible,
        )

    def set_polygon_areas_visibility(
        self,
        *,
        project: "Project",
        area_ids: list[str],
        area_visible: bool | None = None,
        points_visible: bool | None = None,
    ) -> None:
        self.polygon_area_repository_for_project(project).set_areas_visibility(
            area_ids=area_ids,
            area_visible=area_visible,
            points_visible=points_visible,
        )

    @staticmethod
    def polygon_area_overlap_count(areas: list[PolygonArea]) -> int:
        membership_counts: dict[int, int] = {}
        for area in areas:
            for metadata_id in set(area.metadata_ids):
                key = int(metadata_id)
                membership_counts[key] = membership_counts.get(key, 0) + 1
        return sum(1 for count in membership_counts.values() if count > 1)

    def preview_polygon_area_metadata_write(
        self,
        *,
        areas: list[PolygonArea],
        existing_columns: list[str],
        mode: str,
        column_name: str = "",
    ) -> PolygonAreaMetadataWritePreview:
        normalized_mode = str(mode or "").strip().lower()
        if normalized_mode not in {"single_label", "non_mutually_exclusive", "multi_label"}:
            raise ValueError(f"Unsupported area metadata mode: {mode}")
        binary_columns = self._resolved_area_binary_columns(areas)
        existing_by_key = {str(column).casefold(): str(column) for column in existing_columns}
        if normalized_mode == "non_mutually_exclusive":
            candidates = [column for _area_id, column in binary_columns]
        else:
            column = str(column_name or "").strip()
            candidates = [column] if column else []
        overwrite_columns = tuple(
            existing_by_key[column.casefold()]
            for column in candidates
            if column.casefold() in existing_by_key
        )
        return PolygonAreaMetadataWritePreview(
            overlap_count=self.polygon_area_overlap_count(areas),
            binary_columns=tuple(binary_columns),
            overwrite_columns=overwrite_columns,
        )

    def write_polygon_areas_to_metadata(
        self,
        *,
        project: "Project",
        areas: list[PolygonArea],
        column_name: str,
        mode: str,
        priority_area_ids: list[str],
    ) -> PolygonAreaMetadataWriteResult:
        normalized_mode = str(mode or "").strip().lower()
        if normalized_mode not in {"single_label", "non_mutually_exclusive", "multi_label"}:
            raise ValueError(f"Unsupported area metadata mode: {mode}")
        column = str(column_name or "").strip()
        if normalized_mode != "non_mutually_exclusive" and (not column or column.casefold() == "id"):
            raise ValueError("Choose an editable metadata column name.")
        area_by_id = {area.area_id: area for area in areas}
        ordered_areas = [area_by_id[area_id] for area_id in priority_area_ids if area_id in area_by_id]
        ordered_ids = {area.area_id for area in ordered_areas}
        ordered_areas.extend(area for area in areas if area.area_id not in ordered_ids)

        repository = MetadataRepository(project.paths.metadata_database, project.paths.metadata_columns)
        metadata_rows = repository.fetch_rows(order_by="id")
        metadata_ids = {int(row.id) for row in metadata_rows}
        overlap_count = self.polygon_area_overlap_count(areas)
        if normalized_mode == "single_label" and overlap_count > 0 and not priority_area_ids:
            raise ValueError("Choose an area priority for overlapping single-label memberships.")
        label_order = ordered_areas if normalized_mode == "single_label" else list(areas)
        labels_by_id: dict[int, list[str]] = {}
        for area in label_order:
            for raw_id in area.metadata_ids:
                metadata_id = int(raw_id)
                if metadata_id not in metadata_ids:
                    continue
                labels_by_id.setdefault(metadata_id, []).append(area.label)

        existing_columns = {item.name.casefold() for item in repository.list_columns()}
        updates: dict[int, dict[str, str]] = {}
        if normalized_mode == "non_mutually_exclusive":
            binary_columns = self._resolved_area_binary_columns(areas)
            member_ids_by_column = {
                binary_column: {int(value) for value in area_by_id[area_id].metadata_ids}
                for area_id, binary_column in binary_columns
            }
            for metadata_id in metadata_ids:
                updates[metadata_id] = {
                    binary_column: "1" if metadata_id in member_ids else "0"
                    for binary_column, member_ids in member_ids_by_column.items()
                }
            target_columns = [binary_column for _area_id, binary_column in binary_columns]
        else:
            for metadata_id in metadata_ids:
                labels = labels_by_id.get(metadata_id, [])
                value = ""
                if labels:
                    value = labels[0] if normalized_mode == "single_label" else "; ".join(labels)
                updates[metadata_id] = {column: value}
            target_columns = [column]
        repository.merge_add_columns(
            updates,
            [target for target in target_columns if target.casefold() not in existing_columns],
            "",
        )
        overlap_count = sum(1 for labels in labels_by_id.values() if len(labels) > 1)
        affected_count = sum(1 for labels in labels_by_id.values() if labels)
        return PolygonAreaMetadataWriteResult(
            affected_row_count=affected_count,
            unmatched_row_count=max(0, len(metadata_ids) - affected_count),
            overlap_count=overlap_count,
            column_names=tuple(target_columns),
            mode=normalized_mode,
        )

    def write_neighbor_group_to_metadata(
        self,
        *,
        project: "Project",
        column_name: str,
        member_metadata_ids: tuple[int, ...],
    ) -> tuple[str, int]:
        """Store one nearest-neighbor result set as a binary metadata column."""

        requested_column = str(column_name or "").strip()
        if not requested_column or requested_column.casefold() == "id":
            raise ValueError("Choose an editable metadata group label.")

        repository = MetadataRepository(
            project.paths.metadata_database,
            project.paths.metadata_columns,
        )
        columns = repository.list_columns()
        existing_by_key = {column.name.casefold(): column.name for column in columns}
        resolved_column = existing_by_key.get(
            requested_column.casefold(),
            requested_column,
        )
        metadata_ids = set(repository.fetch_ids())
        member_ids = {
            int(metadata_id)
            for metadata_id in member_metadata_ids
            if int(metadata_id) in metadata_ids
        }
        updates = {
            metadata_id: {
                resolved_column: "1" if metadata_id in member_ids else "0"
            }
            for metadata_id in metadata_ids
        }
        repository.merge_add_columns(
            updates,
            [] if resolved_column.casefold() in existing_by_key else [resolved_column],
            "0",
        )
        return resolved_column, len(member_ids)

    @staticmethod
    def _resolved_area_binary_columns(areas: list[PolygonArea]) -> list[tuple[str, str]]:
        output: list[tuple[str, str]] = []
        used: set[str] = {"id"}
        for area in areas:
            base = str(area.label or "").strip() or "area"
            candidate = base
            suffix = 2
            while candidate.casefold() in used:
                candidate = f"{base}_{suffix}"
                suffix += 1
            used.add(candidate.casefold())
            output.append((area.area_id, candidate))
        return output

    def export_polygon_area_members(
        self,
        *,
        project: "Project",
        area: PolygonArea,
        destination: Path,
        export_format: str,
        include_metadata: bool,
    ) -> Path:
        repository = MetadataRepository(project.paths.metadata_database, project.paths.metadata_columns)
        metadata_columns = [item.name for item in repository.list_columns() if item.name != "id"]
        metadata_by_id = {int(row.id): row.values for row in repository.fetch_rows(order_by="id")}
        fieldnames = ["id", *(metadata_columns if include_metadata else [])]
        rows: list[dict[str, object]] = []
        for raw_id in sorted({int(value) for value in area.metadata_ids}):
            row: dict[str, object] = {"id": raw_id}
            if include_metadata:
                values = metadata_by_id.get(raw_id, {})
                row.update({column: str(values.get(column, "")) for column in metadata_columns})
            rows.append(row)
        return PolygonAreaRepository.export_member_rows(
            destination=Path(destination),
            export_format=export_format,
            rows=rows,
            fieldnames=fieldnames,
        )

    def polygon_area_label_exists(
        self,
        *,
        project: "Project",
        discovery_scope: str,
        label: str,
        exclude_area_id: str | None = None,
    ) -> bool:
        return self.polygon_area_repository_for_project(project).area_label_exists(
            discovery_scope=discovery_scope,
            label=label,
            exclude_area_id=exclude_area_id,
        )

    def rename_polygon_area(self, *, project: "Project", area_id: str, label: str) -> None:
        self.polygon_area_repository_for_project(project).rename_area(area_id=area_id, label=label)

    def set_polygon_area_color(self, *, project: "Project", area_id: str, color_hex: str) -> None:
        self.polygon_area_repository_for_project(project).set_area_color(area_id=area_id, color_hex=color_hex)

    def list_pins(self, *, project: "Project", discovery_scope: str) -> list[PinnedAnnotation]:
        return self.pin_repository_for_project(project).list_pins(discovery_scope)

    def list_pin_payloads(self, *, project: "Project", discovery_scope: str) -> list[dict[str, object]]:
        return self.pin_repository_for_project(project).list_pin_payloads(discovery_scope)

    def replace_pin_payloads(
        self,
        *,
        project: "Project",
        discovery_scope: str,
        payloads: list[dict[str, object]],
    ) -> None:
        self.pin_repository_for_project(project).replace_scope_pins(discovery_scope, payloads)

    def project_pin_exists_for_metadata(
        self,
        *,
        project: "Project",
        metadata_id: int,
    ) -> bool:
        return self.pin_repository_for_project(project).project_pin_exists_for_metadata(
            metadata_id
        )

    def pin_exists_for_metadata(
        self,
        *,
        project: "Project",
        metadata_id: int,
    ) -> bool:
        return self.pin_repository_for_project(project).pin_exists_for_metadata(
            metadata_id
        )

    def most_recent_pin_discovery_scope(self, *, project: "Project") -> str | None:
        return self.pin_repository_for_project(project).most_recent_discovery_scope()

    def load_project_discovery_options(self, project: "Project", metadata_ids: np.ndarray) -> list[DiscoveryOption]:
        options = self._repository.list_discovery_options(project)
        saved_options = [option for option in options if not option.is_random]
        random_option = DiscoveryOption(
            key="random",
            label="Random",
            discovery_type="random",
            discovery_file=None,
            parameters={},
            is_random=True,
        )
        return [random_option, *saved_options]

    def load_discovery_dataset_for_option(
        self,
        project: "Project",
        option: DiscoveryOption,
        metadata_ids: np.ndarray,
    ) -> DiscoveryDataset:
        if option.is_random:
            return self._random_discovery_dataset(project, metadata_ids)
        if option.discovery_file is None:
            return DiscoveryRepository.empty()
        return self._repository.load_coordinates(option.discovery_file, metadata_ids)

    def load_discovery_dataset(self, discovery_file, metadata_ids: np.ndarray) -> DiscoveryDataset:
        return self._repository.load_coordinates(discovery_file, metadata_ids)

    def prepare_embedding_neighbor_index(
        self,
        project: "Project",
        option: DiscoveryOption,
        candidate_metadata_ids: np.ndarray,
    ) -> EmbeddingNeighborIndex:
        source_kind = str(option.source.get("kind") or "")
        source_key = str(option.source.get("key") or "")
        if source_kind != "embedding_run" and not source_key.startswith("run:"):
            raise ValueError("The active projection does not have a source embedding.")
        artifact_path = self.embedding_artifact_for_option(project, option)
        if artifact_path is None:
            raise ValueError("The source embedding artifact is unavailable.")
        matrix, object_ids, _summary = load_embedding_matrix_with_ids(artifact_path)
        canonical_ids = self._canonical_artifact_metadata_ids(
            project,
            source_key=source_key,
            artifact_path=artifact_path,
            object_ids=object_ids,
        )
        aligned_matrix, aligned_ids = self._align_vectors_to_metadata(
            matrix,
            canonical_ids,
            np.asarray(candidate_metadata_ids, dtype=np.int64),
        )
        if aligned_ids.size == 0:
            raise ValueError("No active-projection items were found in the source embedding.")
        return build_neighbor_index(aligned_matrix, aligned_ids)

    def list_neighbor_embedding_sources(
        self,
        project: "Project",
    ) -> tuple[NeighborEmbeddingSource, ...]:
        database_path = project.paths.embeddings / "embeddings.db"
        if not database_path.exists():
            return ()
        repository = EmbeddingRunRepository(database_path)
        sources: list[NeighborEmbeddingSource] = []
        for run in repository.list_runs():
            artifact_path = Path(run.artifact_path)
            if not artifact_path.is_absolute():
                artifact_path = project.root / artifact_path
            if (
                run.state != EmbeddingRunState.COMPLETED
                or not run.artifact_path
                or not artifact_path.exists()
            ):
                continue
            item_count = int(run.prepared_input_count) or repository.count_run_inputs(
                run.id
            )
            if item_count > 0:
                sources.append(
                    NeighborEmbeddingSource(
                        run_id=run.id,
                        label=run.label or run.id,
                        item_count=item_count,
                    )
                )
        return tuple(sources)

    def prepare_embedding_neighbor_index_for_run(
        self,
        project: "Project",
        run_id: str,
    ) -> EmbeddingNeighborIndex:
        database_path = project.paths.embeddings / "embeddings.db"
        if not database_path.exists():
            raise ValueError("The selected embedding is unavailable.")
        run = EmbeddingRunRepository(database_path).get_run(str(run_id))
        if run is None or run.state != EmbeddingRunState.COMPLETED or not run.artifact_path:
            raise ValueError("The selected embedding is unavailable.")
        artifact_path = Path(run.artifact_path)
        if not artifact_path.is_absolute():
            artifact_path = project.root / artifact_path
        if not artifact_path.exists():
            raise ValueError("The selected embedding artifact is unavailable.")
        matrix, object_ids, _summary = load_embedding_matrix_with_ids(artifact_path)
        canonical_ids = self._canonical_artifact_metadata_ids(
            project,
            source_key=f"run:{run.id}",
            artifact_path=artifact_path,
            object_ids=object_ids,
        )
        valid = canonical_ids >= 0
        if not np.any(valid):
            raise ValueError("The selected embedding has no usable item identities.")
        return build_neighbor_index(
            np.asarray(matrix[valid], dtype=np.float32),
            np.asarray(canonical_ids[valid], dtype=np.int64),
        )

    def export_discovery_coordinates(
        self,
        project: "Project",
        option: DiscoveryOption,
        metadata_ids: np.ndarray,
        destination: Path,
        export_format: str,
        id_column: str,
        coordinate_source: str = "original",
        arrangement_ref: str | None = None,
    ) -> Path:
        normalized_format = str(export_format or "").strip().lower()
        if normalized_format not in {"csv", "tsv", "json", "xml"}:
            raise ValueError(f"Unsupported discovery export format: {export_format}")
        dataset = self.load_discovery_dataset_for_option(project, option, metadata_ids)
        if str(coordinate_source or "original") == "current":
            from DiscoveryArea.transform_service import DiscoveryTransformService

            transform_service = DiscoveryTransformService()
            if arrangement_ref == "original":
                transformed = None
            elif arrangement_ref:
                transformed = transform_service.load_reference(project, arrangement_ref, dataset)
            else:
                transformed = transform_service.load_current(
                    project, self.discovery_scope_for_option(option), dataset
                )
            if transformed is not None:
                dataset = transform_service.dataset_for_result(transformed)
        if dataset.coordinates.shape[0] == 0:
            raise ValueError("Discovery has no coordinates to export.")
        if dataset.coordinates.shape[0] != dataset.metadata_ids.shape[0]:
            raise ValueError("Discovery coordinates and metadata IDs have different lengths.")
        id_values = self._metadata_id_values_for_export(project, str(id_column or "metadata_id"), dataset.metadata_ids)
        rows: list[dict[str, object]] = []
        for index, metadata_id in enumerate(dataset.metadata_ids.tolist()):
            point = dataset.coordinates[index]
            rows.append(
                {
                    "id": id_values.get(int(metadata_id), ""),
                    "x": float(point[0]),
                    "y": float(point[1]),
                }
            )
        return self._repository.export_coordinates(
            destination=Path(destination),
            export_format=normalized_format,
            rows=rows,
        )

    def list_discovery_methods(self):
        hidden_method_ids = {"custom_coordinates", "random"}
        methods = [
            method
            for method in self._method_catalog.list_methods()
            if method.method_id not in hidden_method_ids
        ]
        return sorted(methods, key=lambda method: (method.method_id != "pca", method.name.casefold()))

    def discovery_supports_arrangement_transform(self, method_id: object) -> bool:
        key = str(method_id or "").strip().lower()
        if key == "t-sne":
            key = "tsne"
        definition = self._method_catalog.get(key)
        return bool(
            definition is not None and definition.supports_arrangement_transform
        )

    def discovery_method_defaults(self, method_id: str) -> dict[str, object]:
        return self._method_catalog.default_parameters(method_id)

    def discovery_method_parameters(self, method_id: str) -> dict[str, dict[str, object]]:
        definition = self._method_catalog.get(method_id)
        return {} if definition is None else definition.parameters

    def create_discovery_from_artifact(
        self,
        project: "Project",
        *,
        artifact_path: Path,
        metadata_ids: np.ndarray,
        label: str,
        method_id: str,
        parameters: dict[str, object] | None = None,
        source_label: str | None = None,
        source_key: str | None = None,
        filter_preset_label: str | None = None,
        filter_preset_snapshot: dict[str, object] | None = None,
        progress_callback=None,
        status_callback=None,
    ) -> DiscoveryOption:
        definition = self._method_catalog.get(method_id)
        if definition is None:
            raise ValueError(f"Unknown discovery method: {method_id}")
        normalized_parameters = self._method_catalog.normalize_parameters(method_id, parameters)
        _emit_status(
            status_callback,
            "Preparing metadata inputs..." if method_id == "random" else "Loading and aligning embedding vectors...",
        )
        if progress_callback is not None:
            progress_callback(5)
        if method_id == "random":
            matrix = np.zeros((metadata_ids.shape[0], 2), dtype=np.float32)
            aligned_ids = np.asarray(metadata_ids, dtype=np.int64)
        else:
            matrix, object_ids, embedding_summary = load_embedding_matrix_with_ids(Path(artifact_path))
            artifact_metadata_ids = self._canonical_artifact_metadata_ids(
                project,
                source_key=source_key,
                artifact_path=artifact_path,
                object_ids=object_ids,
            )
            matrix, aligned_ids = self._align_vectors_to_metadata(
                matrix,
                artifact_metadata_ids,
                metadata_ids,
            )
        if progress_callback is not None:
            progress_callback(30)
        if matrix.shape[0] < 2:
            raise ValueError("Not enough vectors matched project item IDs.")
        finite_mask = _finite_row_mask(matrix)
        if not np.all(finite_mask):
            matrix = np.asarray(matrix[finite_mask], dtype=np.float32)
            aligned_ids = np.asarray(aligned_ids[finite_mask], dtype=np.int64)
        if matrix.shape[0] < 2:
            raise ValueError("Discovery input has fewer than two finite rows.")
        method = self._method_catalog.create(method_id)
        _emit_status(status_callback, f"Running {definition.name} on {matrix.shape[0]:,} items...")
        raw_result = method.fit_transform(matrix, normalized_parameters, progress_callback=progress_callback)
        execution = self._projection_execution(raw_result, normalized_parameters)
        _emit_status(status_callback, "Normalizing projection coordinates...")
        if progress_callback is not None:
            progress_callback(80)
        return self._save_discovery_result(
            project,
            label=label,
            method_id=definition.method_id,
            method_name=definition.name,
            coordinates=np.asarray(execution.coordinates, dtype=np.float32),
            metadata_ids=aligned_ids,
            parameters=normalized_parameters,
            source={
                "kind": "embedding_run",
                "key": str(source_key or ""),
                "label": str(source_label or ""),
                **(
                    {"filter_preset": str(filter_preset_label).strip()}
                    if str(filter_preset_label or "").strip()
                    else {}
                ),
                **self._artifact_source_payload(project, artifact_path),
            },
            description=f"{definition.name} discovery from embeddings.",
            provenance=self._projection_provenance(
                definition=definition,
                execution=execution,
                embedding_summary=embedding_summary if method_id != "random" else {},
                metadata_ids=aligned_ids,
                filter_label=filter_preset_label,
                filter_snapshot=filter_preset_snapshot,
            ),
            progress_callback=progress_callback,
            status_callback=status_callback,
        )

    def create_discovery_from_custom_coordinates(
        self,
        project: "Project",
        *,
        coordinate_file: Path,
        metadata_ids: np.ndarray,
        label: str,
        id_column: str = "metadata_id",
        x_column: str = "x",
        y_column: str = "y",
        progress_callback=None,
        status_callback=None,
    ) -> DiscoveryOption:
        _emit_status(status_callback, "Loading and validating coordinate data...")
        if progress_callback is not None:
            progress_callback(10)
        imported = import_discovery_coordinates(
            Path(coordinate_file),
            metadata_ids=metadata_ids,
            id_column=id_column,
            x_column=x_column,
            y_column=y_column,
        )
        if progress_callback is not None:
            progress_callback(60)
        _emit_status(status_callback, "Normalizing projection coordinates...")
        parameters = {"id_column": id_column, "x_column": x_column, "y_column": y_column}
        definition = self._method_catalog.get("custom_coordinates")
        provenance = None
        if definition is not None:
            provenance = self._projection_provenance(
                definition=definition,
                execution=ProjectionExecutionResult(
                    coordinates=imported.coordinates,
                    requested_parameters=parameters,
                    effective_parameters=parameters,
                    repeatability="deterministic",
                ),
                embedding_summary={},
                metadata_ids=imported.metadata_ids,
                filter_label=None,
                filter_snapshot=None,
            )
        return self._save_discovery_result(
            project,
            label=label,
            method_id="custom_coordinates",
            method_name="Custom Coordinates",
            coordinates=imported.coordinates,
            metadata_ids=imported.metadata_ids,
            parameters=parameters,
            source={"kind": "custom_coordinates", "file_name": Path(coordinate_file).name},
            description="Custom coordinates imported from a user-selected file.",
            provenance=provenance,
            progress_callback=progress_callback,
            status_callback=status_callback,
        )

    def import_discovery_data(
        self,
        project: "Project",
        *,
        coordinate_file: Path,
        metadata_ids: np.ndarray,
        label: str,
        id_column: str,
        x_column: str,
        y_column: str,
        metadata_column: str | None = None,
    ) -> DiscoveryOption:
        imported = import_discovery_coordinates(
            Path(coordinate_file),
            metadata_ids=metadata_ids,
            id_column=id_column,
            x_column=x_column,
            y_column=y_column,
            metadata_column=metadata_column or id_column,
            metadata_key_values_by_id=self._metadata_key_values_for_import(project, metadata_column or id_column),
        )
        parameters = {
            "id_column": id_column,
            "metadata_column": metadata_column or id_column,
            "x_column": x_column,
            "y_column": y_column,
            "matched_rows": imported.validation_report.matched_rows,
            "skipped_invalid_rows": imported.validation_report.skipped_invalid_rows,
            "duplicate_metadata_ids": imported.validation_report.duplicate_metadata_ids,
            "unmatched_metadata_ids": imported.validation_report.unmatched_metadata_ids,
        }
        definition = self._method_catalog.get("custom_coordinates")
        provenance = None
        if definition is not None:
            provenance = self._projection_provenance(
                definition=definition,
                execution=ProjectionExecutionResult(
                    coordinates=imported.coordinates,
                    requested_parameters=parameters,
                    effective_parameters=parameters,
                    repeatability="deterministic",
                ),
                embedding_summary={},
                metadata_ids=imported.metadata_ids,
                filter_label=None,
                filter_snapshot=None,
            )
        return self._save_discovery_result(
            project,
            label=label,
            method_id="imported_coordinates",
            method_name="Imported Coordinates",
            coordinates=imported.coordinates,
            metadata_ids=imported.metadata_ids,
            parameters=parameters,
            source=imported.source_summary,
            description="Imported discovery coordinates from a user-selected file.",
            provenance=provenance,
        )

    def delete_discovery(self, project: "Project", option: DiscoveryOption) -> bool:
        return self._repository.delete_discovery(project, option)

    def discard_discovery_result(self, project: "Project", option_key: str) -> bool:
        """Remove one newly-created projection by its exact preset key."""
        normalized_key = str(Path(option_key).resolve())
        for option in self._repository.list_discovery_options(project):
            if option.is_random or str(Path(option.key).resolve()) != normalized_key:
                continue
            return self._repository.delete_discovery(project, option)
        return False

    def rename_discovery(self, project: "Project", option: DiscoveryOption, label: str) -> bool:
        return self._repository.rename_discovery(project, option, label)

    def list_snapshot_entries(self, project: "Project") -> list[PresetEntry]:
        return self._snapshot_repository(project).list_entries()

    def list_filter_preset_entries(self, project: "Project") -> list[PresetEntry]:
        return PresetRepository(project.paths.filter_presets).list_entries()

    def metadata_ids_for_filter_preset(
        self,
        project: "Project",
        metadata_ids: np.ndarray,
        preset_path: Path,
    ) -> np.ndarray:
        """Return canonical metadata IDs retained by one saved filter preset."""
        requested_path = Path(preset_path).resolve()
        entry = next(
            (
                candidate
                for candidate in self.list_filter_preset_entries(project)
                if candidate.path.resolve() == requested_path
            ),
            None,
        )
        if entry is None:
            raise ValueError("The selected filter preset is no longer available.")
        payload = self.load_filter_preset(project, entry.path)
        if payload is None:
            raise ValueError("The selected filter preset could not be loaded.")

        canonical_ids = np.asarray(metadata_ids, dtype=np.int64).reshape(-1)
        filters = filters_from_state_payload(payload)
        if canonical_ids.size == 0 or not filters:
            return canonical_ids
        columns, numeric_columns = required_filter_columns(filters)
        try:
            text_values, numeric_values = DiscoveryMetadataAccess(project).values_for_ids(
                canonical_ids,
                columns=columns,
                numeric_columns=numeric_columns,
            )
        except Exception as exc:
            raise ValueError("The selected filter preset could not be applied.") from exc
        mask = build_mask(
            filters,
            text_values,
            numeric_values,
            canonical_ids,
            filter_matching_from_state_payload(payload),
        )
        if mask is None:
            return canonical_ids
        return np.ascontiguousarray(canonical_ids[np.asarray(mask, dtype=bool)])

    def list_appearance_preset_entries(self, project: "Project") -> list[PresetEntry]:
        return PresetRepository(project.paths.appearance_presets).list_entries()

    def list_camera_preset_entries(self, project: "Project") -> list[PresetEntry]:
        return PresetRepository(project.paths.camera_presets).list_entries()

    def list_slicer_preset_entries(self, project: "Project") -> list[PresetEntry]:
        return PresetRepository(project.paths.slicer_presets).list_entries()

    def list_transform_preset_entries(self, project: "Project") -> list[PresetEntry]:
        return PresetRepository(project.paths.transform_presets).list_entries()

    def save_filter_preset(self, project: "Project", label: str, payload: dict[str, Any]) -> Path:
        return PresetRepository(project.paths.filter_presets).save_payload(label, payload, allow_overwrite=True)

    def save_appearance_preset(self, project: "Project", label: str, payload: dict[str, Any]) -> Path:
        return PresetRepository(project.paths.appearance_presets).save_payload(label, payload, allow_overwrite=True)

    def save_camera_preset(self, project: "Project", label: str, payload: dict[str, Any]) -> Path:
        return PresetRepository(project.paths.camera_presets).save_payload(label, payload, allow_overwrite=True)

    def save_slicer_preset(self, project: "Project", label: str, payload: dict[str, Any]) -> Path:
        return PresetRepository(project.paths.slicer_presets).save_payload(label, payload, allow_overwrite=True)

    def save_transform_preset(self, project: "Project", label: str, payload: dict[str, Any]) -> Path:
        return PresetRepository(project.paths.transform_presets).save_payload(
            label, payload, allow_overwrite=True
        )

    def ensure_filter_preset(self, project: "Project", label: str, payload: dict[str, Any]) -> Path:
        return PresetRepository(project.paths.filter_presets).ensure_payload(
            label,
            payload,
            ignored_keys=("name",),
        )

    def ensure_appearance_preset(self, project: "Project", label: str, payload: dict[str, Any]) -> Path:
        return PresetRepository(project.paths.appearance_presets).ensure_payload(
            label,
            payload,
            ignored_keys=("name",),
        )

    def ensure_camera_preset(self, project: "Project", label: str, payload: dict[str, Any]) -> Path:
        return PresetRepository(project.paths.camera_presets).ensure_payload(
            label,
            payload,
            ignored_keys=("name",),
        )

    def ensure_slicer_preset(self, project: "Project", label: str, payload: dict[str, Any]) -> Path:
        return PresetRepository(project.paths.slicer_presets).ensure_payload(
            label,
            payload,
            ignored_keys=("name",),
        )

    def ensure_transform_preset(self, project: "Project", label: str, payload: dict[str, Any]) -> Path:
        return PresetRepository(project.paths.transform_presets).ensure_payload(
            label,
            payload,
            ignored_keys=("name",),
        )

    def load_filter_preset(self, project: "Project", path: Path) -> dict[str, Any] | None:
        return PresetRepository(project.paths.filter_presets).load_payload_from_path(path)

    def load_appearance_preset(self, project: "Project", path: Path) -> dict[str, Any] | None:
        return PresetRepository(project.paths.appearance_presets).load_payload_from_path(path)

    def load_camera_preset(self, project: "Project", path: Path) -> dict[str, Any] | None:
        return PresetRepository(project.paths.camera_presets).load_payload_from_path(path)

    def load_slicer_preset(self, project: "Project", path: Path) -> dict[str, Any] | None:
        return PresetRepository(project.paths.slicer_presets).load_payload_from_path(path)

    def load_transform_preset(self, project: "Project", path: Path) -> dict[str, Any] | None:
        return PresetRepository(project.paths.transform_presets).load_payload_from_path(path)

    def save_snapshot(
        self,
        project: "Project",
        label: str,
        snapshot: DiscoverySnapshot,
        dataset: DiscoveryDataset | None = None,
        projection: DiscoverySnapshotProjection | None = None,
    ) -> Path:
        from DiscoveryArea.snapshot_provenance import SnapshotMethodsService

        snapshot = SnapshotMethodsService().freeze(project, snapshot)
        if dataset is None or projection is None:
            return PresetRepository(project.paths.snapshot_presets).save_payload(
                label, snapshot.to_dict(), allow_overwrite=False
            )
        return self._snapshot_repository(project).save(
            label,
            snapshot,
            dataset,
            projection,
            allow_overwrite=False,
        )

    def update_snapshot(
        self,
        project: "Project",
        label: str,
        snapshot: DiscoverySnapshot,
        dataset: DiscoveryDataset | None = None,
        projection: DiscoverySnapshotProjection | None = None,
    ) -> Path:
        from DiscoveryArea.snapshot_provenance import SnapshotMethodsService

        snapshot = SnapshotMethodsService().freeze(project, snapshot)
        if dataset is None or projection is None:
            return PresetRepository(project.paths.snapshot_presets).save_payload(
                label, snapshot.to_dict(), allow_overwrite=True
            )
        return self._snapshot_repository(project).save(
            label,
            snapshot,
            dataset,
            projection,
            allow_overwrite=True,
        )

    def delete_snapshot(self, project: "Project", path: Path) -> bool:
        return self._snapshot_repository(project).delete(path)

    def load_snapshot(self, project: "Project", path: Path) -> DiscoverySnapshot | None:
        return self._snapshot_repository(project).load(path)

    def load_snapshot_coordinates(
        self, project: "Project", snapshot: DiscoverySnapshot
    ) -> DiscoveryDataset | None:
        return self._snapshot_repository(project).load_coordinates(snapshot)

    @staticmethod
    def _snapshot_repository(project: "Project") -> SnapshotRepository:
        return SnapshotRepository(project.root, project.paths.snapshot_presets)

    def list_thumbnail_variants(self, project: "Project") -> list[ThumbnailVariant]:
        return ThumbnailVariantRepository(project.paths.root).list_variants()

    def compute_visibility(
        self,
        filters: list[FilterRule],
        metadata_values: dict[str, np.ndarray],
        metadata_numeric_values: dict[str, np.ndarray],
        metadata_ids: np.ndarray,
        filtered_opacity: float = 0.0,
        matching: FilterMatching = FilterMatching.ALL,
    ) -> np.ndarray:
        mask = build_mask(
            filters,
            metadata_values,
            metadata_numeric_values,
            metadata_ids,
            matching,
        )
        if mask is None:
            return np.ones(metadata_ids.shape[0], dtype=np.float32)
        clamped = float(np.clip(filtered_opacity, 0.0, 1.0))
        if clamped <= 0.0:
            return mask.astype(np.float32)
        return np.where(mask, 1.0, clamped).astype(np.float32)

    @staticmethod
    def draw_order_for_visibility(
        visibility: np.ndarray, prioritize_last: int | None = None
    ) -> np.ndarray:
        if visibility.size == 0:
            return np.empty((0,), dtype=np.int64)
        order = np.argsort(visibility, kind="stable")
        if prioritize_last is not None and 0 <= prioritize_last < visibility.shape[0]:
            order = order[order != prioritize_last]
            order = np.concatenate([order, np.array([int(prioritize_last)], dtype=order.dtype)])
        return order

    def _random_discovery_dataset(self, project: "Project", metadata_ids: np.ndarray) -> DiscoveryDataset:
        """A disposable view, stable per item and never published as an analysis."""
        ids = np.asarray(metadata_ids, dtype=np.int64)
        media_database = project.paths.media / "media_index.db"
        if ids.size == 0 or not media_database.is_file():
            return DiscoveryRepository.empty()
        linked_ids = MediaLinkRepository(media_database).linked_metadata_ids()
        ids = ids[np.isin(ids, linked_ids)]
        if ids.size == 0:
            return DiscoveryRepository.empty()
        # SplitMix64 gives each ID a stable position, independent of row order,
        # collection size, process hash randomization, or metadata edits.
        values = ids.astype(np.uint64)[:, None] + np.array(
            [0x9E3779B97F4A7C15, 0x3C6EF372FE94F82A], dtype=np.uint64
        )
        values = (values ^ (values >> 30)) * np.uint64(0xBF58476D1CE4E5B9)
        values = (values ^ (values >> 27)) * np.uint64(0x94D049BB133111EB)
        values ^= values >> 31
        coordinates = ((values >> 40).astype(np.float32) / (1 << 23) - 1).astype(np.float32)
        return DiscoveryDataset(coordinates=coordinates, metadata_ids=ids.copy())

    def create_random_discovery(
        self,
        project: "Project",
        metadata_ids: np.ndarray,
        label: str,
    ) -> DiscoveryOption:
        if metadata_ids.size == 0:
            raise ValueError("Cannot create a projection without items.")
        seed = 42
        coordinates = np.random.default_rng(seed).random((metadata_ids.size, 2), dtype=np.float32)
        definition = self._method_catalog.get("random")
        provenance = None
        if definition is not None:
            provenance = self._projection_provenance(
                definition=definition,
                execution=ProjectionExecutionResult(
                    coordinates=coordinates,
                    requested_parameters={"random_state": seed},
                    effective_parameters={"random_state": seed},
                    repeatability="deterministic",
                ),
                embedding_summary={},
                metadata_ids=metadata_ids,
                filter_label=None,
                filter_snapshot=None,
            )
        return self._save_discovery_result(
            project,
            label=label.strip() or "Random Discovery",
            method_id="random",
            method_name="Random",
            coordinates=coordinates,
            metadata_ids=np.asarray(metadata_ids, dtype=np.int64),
            parameters={"source": "wizard_random", "random_state": seed},
            source={"kind": "metadata", "label": "Current metadata"},
            description="Random coordinates generated from metadata IDs.",
            provenance=provenance,
        )

    def create_pca_discovery_from_artifact(
        self,
        project: "Project",
        *,
        artifact_path: Path,
        metadata_ids: np.ndarray,
        label: str,
    ) -> DiscoveryOption:
        return self.create_discovery_from_artifact(
            project,
            artifact_path=artifact_path,
            metadata_ids=metadata_ids,
            label=label.strip() or "PCA Discovery",
            method_id="pca",
            parameters={},
            source_label=None,
        )

    def _save_discovery_result(
        self,
        project: "Project",
        *,
        label: str,
        method_id: str,
        method_name: str,
        coordinates: np.ndarray,
        metadata_ids: np.ndarray,
        parameters: dict[str, object],
        source: dict[str, object],
        description: str,
        provenance: dict[str, object] | None = None,
        progress_callback=None,
        status_callback=None,
    ) -> DiscoveryOption:
        coordinates = np.asarray(coordinates, dtype=np.float32)
        metadata_ids = np.asarray(metadata_ids, dtype=np.int64)
        finite_mask = _finite_row_mask(coordinates)
        if not np.all(finite_mask):
            coordinates = coordinates[finite_mask]
            metadata_ids = metadata_ids[finite_mask]
        if coordinates.shape[0] < 2:
            raise ValueError("Discovery produced fewer than two finite coordinate rows.")
        normalized, normalization = self.normalize_coordinates_for_canvas(coordinates)
        stored_provenance = dict(provenance or {})
        if stored_provenance:
            stored_provenance["normalization"] = normalization
            selection = dict(stored_provenance.get("selection") or {})
            selection["item_count"] = int(len(metadata_ids))
            selection["metadata_checksum"] = checksum_payload([int(value) for value in metadata_ids])
            stored_provenance["selection"] = selection
        _emit_status(status_callback, "Saving projection coordinates in one batch...")
        if progress_callback is not None:
            progress_callback(85)
        option = self._repository.save_discovery(
            project,
            label=label,
            method_id=method_id,
            method_name=method_name,
            coordinates=normalized,
            metadata_ids=metadata_ids,
            parameters=parameters,
            normalization=normalization,
            source=source,
            provenance=stored_provenance,
            description=description,
        )
        if progress_callback is not None:
            progress_callback(92)
        _emit_status(status_callback, "Projection files saved.")
        return option

    def normalize_coordinates_for_canvas(self, coordinates: np.ndarray) -> tuple[np.ndarray, dict[str, object]]:
        raw = np.asarray(coordinates, dtype=np.float32)
        if raw.ndim != 2 or raw.shape[1] < 2:
            raise ValueError("Discovery coordinates must contain x and y values.")
        raw = raw[:, :2]
        min_xy = np.min(raw, axis=0)
        max_xy = np.max(raw, axis=0)
        center = (min_xy + max_xy) * 0.5
        span = max_xy - min_xy
        largest_span = float(np.max(span))
        scale = 1.0 if largest_span <= 1e-12 else 2.0 / largest_span
        normalized = (raw - center) * scale
        if span[0] <= 1e-12:
            normalized[:, 0] = 0.0
        if span[1] <= 1e-12:
            normalized[:, 1] = 0.0
        return normalized.astype(np.float32), {
            "version": 1,
            "original_min": [float(min_xy[0]), float(min_xy[1])],
            "original_max": [float(max_xy[0]), float(max_xy[1])],
            "center": [float(center[0]), float(center[1])],
            "scale": float(scale),
        }

    def methods_report(self, option: DiscoveryOption):
        return ProjectionProvenanceReportService().build(option.label, option.provenance)

    @staticmethod
    def _projection_execution(
        value: np.ndarray | ProjectionExecutionResult,
        requested: dict[str, object],
    ) -> ProjectionExecutionResult:
        if isinstance(value, ProjectionExecutionResult):
            return value
        return ProjectionExecutionResult(
            coordinates=np.asarray(value, dtype=np.float32),
            requested_parameters=dict(requested),
            effective_parameters=dict(requested),
            repeatability="unknown",
            complete=False,
        )

    @staticmethod
    def _projection_provenance(
        *,
        definition,
        execution: ProjectionExecutionResult,
        embedding_summary: dict[str, object],
        metadata_ids: np.ndarray,
        filter_label: str | None,
        filter_snapshot: dict[str, object] | None,
    ) -> dict[str, object]:
        distribution = str(definition.implementation.get("distribution") or "eidora-desktop")
        try:
            implementation_version = importlib_metadata.version(distribution)
        except importlib_metadata.PackageNotFoundError:
            implementation_version = EIDORA_VERSION if distribution == "eidora-desktop" else "not recorded"
        embedded = dict(embedding_summary or {})
        from EmbeddingsArea.provenance import embedding_citation_payloads

        runtime_provenance = dict(embedded.get("provenance") or {})
        model_citations = embedding_citation_payloads(embedded)
        method_citations = [
            *[dict(value) for value in definition.citations],
            *[dict(value) for value in execution.citations],
        ]
        missing: list[str] = []
        if definition.method_id not in {"random", "custom_coordinates"} and not method_citations:
            missing.append(f"No citation was recorded for projection method {definition.name}.")
        if embedded and not model_citations:
            missing.append("No model citation was recorded in the source embedding package.")
        if not execution.complete:
            missing.append("The projection method did not report runtime-resolved effective parameters.")
        filter_payload = dict(filter_snapshot or {})
        return {
            "schema_version": PROVENANCE_SCHEMA_VERSION,
            "eidora_version": EIDORA_VERSION,
            "citations": [eidora_citation_payload(), *method_citations],
            "embedding": {
                "model_id": str(embedded.get("model_id") or ""),
                "model_name": str(embedded.get("model_name") or ""),
                "model_version": str(runtime_provenance.get("model_version") or ""),
                "package_fingerprint": str(runtime_provenance.get("package_fingerprint") or runtime_provenance.get("model_sha256") or ""),
                "input_signature": str(runtime_provenance.get("input_signature") or runtime_provenance.get("prepared_signature") or ""),
                "preprocessing": embedded.get("preprocess") or {},
                "parameters": embedded.get("parameter_values") or {},
                "postprocessing": embedded.get("postprocess") or [],
                "execution_providers": list(runtime_provenance.get("execution_providers") or []),
                "final_execution_mode": str(runtime_provenance.get("final_execution_mode") or ""),
                "citations": model_citations,
            },
            "selection": {
                "item_count": int(len(metadata_ids)),
                "metadata_checksum": checksum_payload([int(value) for value in np.asarray(metadata_ids)]),
                **({
                    "filter": {
                        "label": str(filter_label or filter_payload.get("name") or "Recorded filter"),
                        "rules": filter_payload,
                        "checksum": checksum_payload(filter_payload),
                    }
                } if filter_payload else {}),
            },
            "method": {
                "id": definition.method_id,
                "name": definition.name,
                "implementation": distribution,
                "implementation_version": implementation_version,
                "requested_parameters": dict(execution.requested_parameters),
                "effective_parameters": dict(execution.effective_parameters),
                "derived_steps": list(execution.derived_steps),
                "repeatability": execution.repeatability,
            },
            "missing": missing,
        }

    @staticmethod
    def _align_vectors_to_metadata(
        matrix: np.ndarray,
        object_ids: np.ndarray,
        metadata_ids: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray]:
        data = np.asarray(matrix, dtype=np.float32)
        ids = np.asarray(object_ids, dtype=np.int64)
        wanted = np.asarray(metadata_ids, dtype=np.int64)
        if ids.shape == wanted.shape and np.array_equal(ids, wanted):
            return data, wanted
        if ids.ndim != 1 or wanted.ndim != 1 or data.ndim != 2 or len(ids) != len(data):
            raise ValueError("Embedding IDs and vectors are not aligned.")
        order = np.argsort(ids, kind="stable")
        sorted_ids = ids[order]
        positions = np.searchsorted(sorted_ids, wanted, side="left")
        valid = positions < len(sorted_ids)
        valid_indices = np.flatnonzero(valid)
        if valid_indices.size:
            valid[valid_indices] = sorted_ids[positions[valid_indices]] == wanted[valid_indices]
        if not np.any(valid):
            return np.empty((0, data.shape[1] if data.ndim == 2 else 0), dtype=np.float32), np.empty((0,), dtype=np.int64)
        rows = order[positions[valid]]
        return np.asarray(data[rows], dtype=np.float32), np.asarray(wanted[valid], dtype=np.int64)

    def _canonical_artifact_metadata_ids(
        self,
        project: "Project",
        *,
        source_key: str | None,
        artifact_path: Path,
        object_ids: np.ndarray,
    ) -> np.ndarray:
        """Translate run-local artifact ordinals to canonical metadata row IDs."""

        ids = np.asarray(object_ids, dtype=np.int64).reshape(-1)
        run_id = self._embedding_run_id(
            source_key=source_key,
            artifact_path=artifact_path,
        )
        identity = self._embedding_run_identity(project, run_id)
        if identity is None or ids.size == 0:
            return ids
        known_ordinals, metadata_id_by_ordinal = identity
        if not all(int(value) in known_ordinals for value in ids.tolist()):
            return ids
        return np.asarray(
            [metadata_id_by_ordinal.get(int(value), -1) for value in ids.tolist()],
            dtype=np.int64,
        )

    @staticmethod
    def _embedding_run_id(
        *,
        source_key: str | None,
        artifact_path: Path | None,
    ) -> str | None:
        key = str(source_key or "").strip()
        if key.startswith("run:"):
            return key.removeprefix("run:").strip() or None
        if key.startswith("run_"):
            return key
        if artifact_path is not None:
            stem = Path(artifact_path).stem
            if stem.startswith("run_"):
                return stem
        return None

    @staticmethod
    def _embedding_run_identity(
        project: "Project",
        run_id: str | None,
    ) -> tuple[set[int], dict[int, int]] | None:
        if not run_id:
            return None
        database_path = project.paths.embeddings / "embeddings.db"
        if not database_path.exists():
            return None
        repository = EmbeddingRunRepository(database_path)
        known_ordinals, metadata_id_by_ordinal = repository.input_metadata_ids_by_ordinal(
            run_id
        )
        if not known_ordinals or not metadata_id_by_ordinal:
            return None
        return known_ordinals, metadata_id_by_ordinal

    @staticmethod
    def _project_artifact_path(
        project: "Project",
        source: dict[str, object],
    ) -> Path | None:
        raw = str(source.get("artifact_path") or "").strip()
        if not raw:
            return None
        path = Path(raw)
        return path if path.is_absolute() else project.root / path

    def embedding_artifact_for_option(
        self,
        project: "Project",
        option: DiscoveryOption,
    ) -> Path | None:
        path = self._project_artifact_path(project, option.source)
        if path is not None and path.exists():
            return path
        run_id = self._embedding_run_id(
            source_key=str(option.source.get("key") or ""),
            artifact_path=path,
        )
        if not run_id:
            return None
        database_path = project.paths.embeddings / "embeddings.db"
        if not database_path.exists():
            return None
        run = EmbeddingRunRepository(database_path).get_run(run_id)
        if run is None or not run.artifact_path:
            return None
        artifact = Path(run.artifact_path)
        return artifact if artifact.exists() else None

    @staticmethod
    def _artifact_source_payload(project: "Project", artifact_path: Path) -> dict[str, object]:
        path = Path(artifact_path)
        try:
            return {"artifact_path": str(path.resolve().relative_to(project.root.resolve()))}
        except ValueError:
            return {"external_artifact_name": path.name}

    @staticmethod
    def _metadata_key_values_for_import(project: "Project", column_name: str) -> dict[int, str] | None:
        column = str(column_name or "").strip()
        if not column or column in {"metadata_id", "id"}:
            return None
        repository = MetadataRepository(project.paths.metadata_database, project.paths.metadata_columns)
        try:
            names = {item.name for item in repository.list_columns()}
        except Exception:
            return None
        if column not in names:
            return None
        return repository.column_values_by_id(column)

    @staticmethod
    def _metadata_id_values_for_export(
        project: "Project",
        column_name: str,
        metadata_ids: np.ndarray,
    ) -> dict[int, str]:
        column = str(column_name or "").strip()
        ids = [int(value) for value in np.asarray(metadata_ids, dtype=np.int64).tolist()]
        if not column or column in {"metadata_id", "id"}:
            return {metadata_id: str(metadata_id) for metadata_id in ids}
        repository = MetadataRepository(project.paths.metadata_database, project.paths.metadata_columns)
        names = {item.name for item in repository.list_columns()}
        if column not in names:
            raise ValueError(f"Metadata column does not exist: {column}")
        values = repository.column_values_by_id(column)
        return {metadata_id: str(values.get(metadata_id, "")) for metadata_id in ids}


def _slugify(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return text or "discovery"


def _finite_row_mask(matrix: np.ndarray) -> np.ndarray:
    data = np.asarray(matrix)
    if data.ndim != 2:
        return np.empty((0,), dtype=bool)
    return np.isfinite(data).all(axis=1)


def _emit_status(callback, message: str) -> None:
    if callback is not None:
        callback(message)


ProjectionService = DiscoveryService
ProjectionsService = DiscoveryService
