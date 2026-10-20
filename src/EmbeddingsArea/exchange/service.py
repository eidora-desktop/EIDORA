"""Import validation and publication through the existing run identity contract."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import uuid

import numpy as np

from EmbeddingsArea.domain import EmbeddingRun, EmbeddingRunInputSnapshot, EmbeddingRunState, MediaContentType
from EmbeddingsArea.exchange.domain import (
    EmbeddingImportCompanionProbe,
    EmbeddingImportInspection,
    EmbeddingImportInterpretation,
    EmbeddingImportPlan,
    EmbeddingImportProbe,
    EmbeddingImportSample,
)
from EmbeddingsArea.exchange import formats
from EmbeddingsArea.exchange.importer import (
    EmbeddingImportCancelled,
    create_staging_directory,
    identifier_database,
    iter_source_chunks,
    probe_identifier_source,
    probe_source,
    source_fingerprint,
)
from EmbeddingsArea.runtime.artifacts import ArtifactStore
from EmbeddingsArea.runtime.readers import load_embeddings_payload_with_ids


LOGGER = logging.getLogger(__name__)


class EmbeddingExchangeService:
    def __init__(self, project, runs, metadata):
        self.project, self.runs, self.metadata = project, runs, metadata

    def probe_import(
        self, path: Path, *, sidecar_path: Path | None = None
    ) -> EmbeddingImportProbe:
        return probe_source(path, sidecar_path=sidecar_path)

    def probe_import_identifiers(self, path: Path) -> EmbeddingImportCompanionProbe:
        return probe_identifier_source(path)

    def prepare_import(
        self,
        probe: EmbeddingImportProbe,
        interpretation: EmbeddingImportInterpretation,
        *,
        progress_callback: Callable[[int, str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> EmbeddingImportInspection:
        """Validate a source in bounded chunks and stage a normalized copy."""

        staging = create_staging_directory(self.project.paths.embeddings)
        store = ArtifactStore(staging)
        identity_connection = None
        row_count = 0
        dimensions = 0
        dtype = ""
        source_has_identifiers = False
        samples: list[EmbeddingImportSample] = []
        identifier_fingerprint = (
            source_fingerprint(interpretation.identifier_path)
            if interpretation.identifier_path is not None
            else None
        )
        sidecar_fingerprint = (
            source_fingerprint(interpretation.sidecar_path)
            if interpretation.sidecar_path is not None
            else None
        )
        try:
            identity_connection = identifier_database(staging / "identifiers.sqlite")
            for chunk_index, (raw_matrix, raw_identifiers) in enumerate(
                iter_source_chunks(
                    probe,
                    interpretation,
                    staging,
                    should_cancel=should_cancel,
                )
            ):
                if should_cancel is not None and should_cancel():
                    raise EmbeddingImportCancelled("Embedding import was aborted.")
                matrix = np.asarray(raw_matrix)
                if matrix.ndim != 2 or not len(matrix) or matrix.dtype.kind not in "fiu":
                    raise ValueError("Select a nonempty two-dimensional numeric vector matrix.")
                if not np.isfinite(matrix).all():
                    raise ValueError("Vectors contain NaN or infinity. Repair them before importing.")
                if not dimensions:
                    dimensions = int(matrix.shape[1])
                    dtype = matrix.dtype.str
                    store.init_manifest(
                        {
                            "source": "import",
                            "dtype": dtype,
                            "provenance": dict(probe.provenance),
                        }
                    )
                elif matrix.shape[1] != dimensions:
                    raise ValueError("Embedding rows have inconsistent dimensions.")

                if raw_identifiers is None:
                    identifiers = [
                        f"vector_{index + 1:06d}"
                        for index in range(row_count, row_count + len(matrix))
                    ]
                else:
                    source_has_identifiers = True
                    if len(raw_identifiers) != len(matrix):
                        raise ValueError("Identifier count must equal vector count.")
                    identifiers = [
                        self._normalize_import_identifier(value)
                        for value in raw_identifiers
                    ]
                if any(not value for value in identifiers):
                    raise ValueError("Identifiers must not be empty.")
                try:
                    identity_connection.executemany(
                        "INSERT INTO identifiers (row_index, identifier) VALUES (?, ?)",
                        ((row_count + index, value) for index, value in enumerate(identifiers)),
                    )
                    identity_connection.commit()
                except sqlite3.IntegrityError as exc:
                    raise ValueError(
                        "Duplicate embedding identifiers are ambiguous after trimming whitespace."
                    ) from exc

                object_ids = np.arange(row_count, row_count + len(matrix), dtype=np.int64)
                store.append_chunk(
                    chunk_index,
                    object_ids,
                    matrix,
                    np.asarray(identifiers),
                )
                for offset in range(min(len(matrix), 25 - len(samples))):
                    samples.append(
                        EmbeddingImportSample(
                            row_index=row_count + offset,
                            identifier=identifiers[offset],
                            vector=tuple(float(value) for value in matrix[offset, :8]),
                        )
                    )
                row_count += len(matrix)
                if progress_callback is not None:
                    hint = probe.row_count_hint
                    value = min(95, int(row_count / hint * 95)) if hint else 0
                    progress_callback(value, f"Validated {row_count:,} vectors…")

            if not row_count:
                raise ValueError("The selected embedding file contains no vectors.")
            inspection = EmbeddingImportInspection(
                probe=probe,
                interpretation=interpretation,
                staging_path=staging,
                row_count=row_count,
                dimensions=dimensions,
                dtype=np.dtype(dtype).name,
                source_has_identifiers=source_has_identifiers,
                samples=tuple(samples),
                identifier_fingerprint=identifier_fingerprint,
                sidecar_fingerprint=sidecar_fingerprint,
            )
            complete_columns = self._complete_import_match_columns(inspection)
            if progress_callback is not None:
                progress_callback(100, "Embedding file is ready to link.")
            return replace(inspection, complete_metadata_columns=complete_columns)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        finally:
            if identity_connection is not None:
                identity_connection.close()

    def preview_import_links(
        self,
        inspection: EmbeddingImportInspection,
        *,
        strategy: str,
        metadata_column: str = "",
        allow_partial: bool = False,
    ) -> EmbeddingImportPlan:
        if strategy not in {"identifier", "row_order", "create_metadata"}:
            raise ValueError("Choose a metadata linking strategy.")
        row_count = inspection.row_count
        match_path: Path | None = inspection.staging_path / "metadata_ids.npy"
        fingerprint = ""

        if strategy == "create_metadata":
            if self.metadata.is_initialized() and self.metadata.count_rows():
                raise ValueError("Metadata creation is available only in an empty project.")
            match_path = None
            matched = selected = row_count
            unmatched = 0
            unmatched_metadata = 0
            message = f"Create {row_count:,} metadata rows and import every vector."
            can_import = True
        elif strategy == "row_order":
            metadata_ids = self.metadata.fetch_ids()
            if len(metadata_ids) != row_count:
                raise ValueError(
                    "Row-order matching requires exactly the same number of metadata rows and vectors."
                )
            np.save(match_path, np.asarray(metadata_ids, dtype=np.int64))
            matched = selected = row_count
            unmatched = 0
            unmatched_metadata = 0
            message = f"Link all {row_count:,} vectors by canonical metadata row order."
            can_import = True
            fingerprint = self._row_order_fingerprint(metadata_ids)
        else:
            if not inspection.source_has_identifiers:
                raise ValueError(
                    "This file has no identifiers. Choose an identifier file or use row-order matching."
                )
            if not metadata_column:
                raise ValueError("Choose the project metadata identifier column.")
            allowed_columns = set(self.metadata.complete_unique_column_names())
            if metadata_column not in allowed_columns:
                raise ValueError("Choose a complete metadata column with unique identifiers.")
            matches = np.lib.format.open_memmap(
                match_path, mode="w+", dtype=np.int64, shape=(row_count,)
            )
            matches[:] = -1
            matched = 0
            quoted = '"' + metadata_column.replace('"', '""') + '"'
            with closing(sqlite3.connect(inspection.staging_path / "identifiers.sqlite")) as connection:
                connection.execute(
                    "ATTACH DATABASE ? AS project_metadata",
                    (str(self.metadata.database_path),),
                )
                rows = connection.execute(
                    f"""
                    SELECT imported.row_index, project.id
                    FROM project_metadata.metadata AS project
                    JOIN identifiers AS imported
                      ON imported.identifier =
                         TRIM(COALESCE(CAST(project.{quoted} AS TEXT), ''))
                    """
                )
                for row_index, metadata_row_id in rows:
                    metadata_id = -1 if metadata_row_id is None else int(metadata_row_id)
                    matches[int(row_index)] = metadata_id
                    matched += metadata_id >= 0
            matches.flush()
            unmatched = row_count - matched
            unmatched_metadata = self.metadata.count_rows() - matched
            selected = matched if allow_partial else row_count
            can_import = matched > 0 and (unmatched == 0 or allow_partial)
            fingerprint = self.metadata.column_fingerprint(metadata_column)
            if unmatched and not allow_partial:
                message = (
                    f"{matched:,} matched and {unmatched:,} unmatched. "
                    "Repair the mapping or explicitly import matched vectors only."
                )
            elif unmatched:
                message = f"Import {matched:,} matched vectors and skip {unmatched:,} unmatched vectors."
            else:
                message = f"All {matched:,} vectors match project metadata."
            if unmatched_metadata:
                message += (
                    f" {unmatched_metadata:,} metadata rows have no imported vector."
                )

        return EmbeddingImportPlan(
            inspection=inspection,
            strategy=strategy,  # type: ignore[arg-type]
            metadata_column=metadata_column,
            match_index_path=match_path,
            metadata_fingerprint=fingerprint,
            matched_count=matched,
            unmatched_count=unmatched,
            unmatched_metadata_count=unmatched_metadata,
            selected_count=selected,
            allow_partial=allow_partial,
            can_import=can_import,
            message=message,
        )

    def commit_import(
        self,
        plan: EmbeddingImportPlan,
        *,
        label: str,
        progress_callback: Callable[[int, str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> EmbeddingRun:
        if not plan.can_import or not plan.selected_count:
            raise ValueError(plan.message or "The import plan is not valid.")
        inspection = plan.inspection
        try:
            current_source_fingerprint = source_fingerprint(
                inspection.probe.source_path
            )
        except OSError:
            current_source_fingerprint = None
        if current_source_fingerprint != inspection.probe.source_fingerprint:
            self.cleanup_import(inspection)
            raise ValueError("The source file changed after inspection. Inspect it again.")
        identifier_path = inspection.interpretation.identifier_path
        if identifier_path is not None:
            try:
                current_identifier_fingerprint = source_fingerprint(identifier_path)
            except OSError:
                current_identifier_fingerprint = None
            if current_identifier_fingerprint != inspection.identifier_fingerprint:
                self.cleanup_import(inspection)
                raise ValueError("The identifier file changed after inspection. Inspect it again.")
        sidecar_path = inspection.interpretation.sidecar_path
        if sidecar_path is not None:
            try:
                current_sidecar_fingerprint = source_fingerprint(sidecar_path)
            except OSError:
                current_sidecar_fingerprint = None
            if current_sidecar_fingerprint != inspection.sidecar_fingerprint:
                self.cleanup_import(inspection)
                raise ValueError("The schema sidecar changed after inspection. Inspect it again.")
        if plan.strategy == "identifier":
            current = self.metadata.column_fingerprint(plan.metadata_column)
            if current != plan.metadata_fingerprint:
                self.cleanup_import(inspection)
                raise ValueError("Metadata changed after matching. Match the identifiers again.")
        elif plan.strategy == "row_order":
            if self._row_order_fingerprint(self.metadata.fetch_ids()) != plan.metadata_fingerprint:
                self.cleanup_import(inspection)
                raise ValueError("Metadata changed after matching. Match the rows again.")
        elif self.metadata.is_initialized() and self.metadata.count_rows():
            self.cleanup_import(inspection)
            raise ValueError("Metadata changed after inspection. Inspect the import again.")

        run_id = f"run_{uuid.uuid4().hex}"
        output = self.project.paths.embeddings / f"{run_id}.npz"
        run_dir = self.project.paths.embeddings / ".runs" / run_id
        store = ArtifactStore(run_dir)
        published = False
        try:
            store.init_manifest(
                {
                    "source": "import",
                    "dtype": np.dtype(inspection.dtype).str,
                    "provenance": dict(inspection.probe.provenance),
                }
            )
            selected_rows = 0
            matches = (
                np.load(plan.match_index_path, mmap_mode="r")
                if plan.match_index_path is not None
                else None
            )
            staged = ArtifactStore(inspection.staging_path)
            source_offset = 0
            for source_ids, vectors, identifiers in staged.iter_chunks_with_names():
                if should_cancel is not None and should_cancel():
                    raise EmbeddingImportCancelled("Embedding import was aborted.")
                start = source_offset
                stop = start + len(source_ids)
                expected_ids = np.arange(start, stop, dtype=np.int64)
                if not np.array_equal(source_ids, expected_ids):
                    raise RuntimeError(
                        "Staged import chunk identities are inconsistent. Inspect the file again."
                    )
                source_offset = stop
                mask = (
                    np.ones(len(source_ids), dtype=bool)
                    if matches is None
                    else np.asarray(matches[start:stop]) >= 0
                )
                if not np.any(mask):
                    continue
                chosen_vectors = np.asarray(vectors[mask])
                chosen_names = np.asarray(identifiers[mask])
                object_ids = np.arange(
                    selected_rows, selected_rows + len(chosen_vectors), dtype=np.int64
                )
                store.append_chunk(
                    store.chunk_count(), object_ids, chosen_vectors, chosen_names
                )
                selected_rows += len(chosen_vectors)
                if progress_callback is not None:
                    progress_callback(
                        min(75, int(selected_rows / plan.selected_count * 75)),
                        f"Prepared {selected_rows:,} of {plan.selected_count:,} vectors…",
                    )
            if selected_rows != plan.selected_count:
                raise RuntimeError("Prepared vectors do not match the validated import plan.")
            if progress_callback is not None:
                progress_callback(80, "Finalizing the embedding artifact…")
            store.finalize_npz(
                output,
                model_id="",
                model_label=str(
                    inspection.probe.provenance.get("model_name", "Imported embeddings")
                ),
                backend="import",
                input_count=selected_rows,
                check_cancelled=lambda: self._raise_if_import_cancelled(
                    should_cancel
                ),
            )
            now = datetime.now(timezone.utc).isoformat()
            run = EmbeddingRun(
                run_id,
                label.strip() or inspection.probe.source_path.stem,
                EmbeddingRunState.COMPLETED,
                progress=100,
                prepared_input_count=selected_rows,
                processed_items=selected_rows,
                created_at=now,
                updated_at=now,
                artifact_path=str(output),
            )
            inputs = self._iter_import_inputs(plan, run_id)
            create_identifiers = (
                self._iter_selected_identifiers(plan)
                if plan.strategy == "create_metadata"
                else None
            )
            self.runs.save_imported_run(
                run,
                inputs,
                metadata_database=self.metadata.database_path,
                create_identifiers=create_identifiers,
                metadata_column=(
                    plan.metadata_column if plan.strategy == "identifier" else ""
                ),
                expected_metadata_fingerprint=plan.metadata_fingerprint,
                expected_row_order_fingerprint=(
                    plan.metadata_fingerprint if plan.strategy == "row_order" else ""
                ),
            )
            published = True
            if plan.strategy == "create_metadata":
                try:
                    self.metadata.recompute_column_summaries()
                except Exception:
                    LOGGER.exception(
                        "Imported embeddings, but metadata summary refresh failed"
                    )
            if progress_callback is not None:
                progress_callback(100, "Embeddings imported.")
            return run
        finally:
            if not published:
                output.unlink(missing_ok=True)
                shutil.rmtree(run_dir, ignore_errors=True)
            self.cleanup_import(inspection)

    def cleanup_import(self, inspection: EmbeddingImportInspection | None) -> None:
        if inspection is not None:
            shutil.rmtree(inspection.staging_path, ignore_errors=True)

    @staticmethod
    def _raise_if_import_cancelled(
        should_cancel: Callable[[], bool] | None,
    ) -> None:
        if should_cancel is not None and should_cancel():
            raise EmbeddingImportCancelled("Embedding import was aborted.")

    @staticmethod
    def _normalize_import_identifier(value) -> str:
        if isinstance(value, (bytes, np.bytes_)):
            return bytes(value).decode("utf-8").strip()
        return str(value).strip()

    def _complete_import_match_columns(
        self, inspection: EmbeddingImportInspection
    ) -> tuple[str, ...]:
        if not inspection.source_has_identifiers or not self.metadata.is_initialized():
            return ()
        complete: list[str] = []
        with closing(sqlite3.connect(inspection.staging_path / "identifiers.sqlite")) as connection:
            connection.execute(
                "ATTACH DATABASE ? AS project_metadata",
                (str(self.metadata.database_path),),
            )
            for _label, column in self.list_import_identifier_columns():
                quoted = '"' + column.replace('"', '""') + '"'
                matched = connection.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM project_metadata.metadata AS project
                    JOIN identifiers AS imported
                      ON imported.identifier =
                         TRIM(COALESCE(CAST(project.{quoted} AS TEXT), ''))
                    """
                ).fetchone()[0]
                if int(matched) == inspection.row_count:
                    complete.append(column)
        return tuple(complete)

    @staticmethod
    def _row_order_fingerprint(row_ids) -> str:
        import hashlib

        digest = hashlib.sha256()
        for row_id in row_ids:
            digest.update(str(int(row_id)).encode("ascii"))
            digest.update(b"\n")
        return digest.hexdigest()

    def _iter_selected_identifiers(
        self, plan: EmbeddingImportPlan
    ) -> Iterator[str]:
        for item in self._iter_import_inputs(plan, ""):
            yield item.source_key

    def _iter_import_inputs(
        self, plan: EmbeddingImportPlan, run_id: str
    ) -> Iterator[EmbeddingRunInputSnapshot]:
        matches = (
            np.load(plan.match_index_path, mmap_mode="r")
            if plan.match_index_path is not None
            else None
        )
        ordinal = 0
        source_offset = 0
        staged = ArtifactStore(plan.inspection.staging_path)
        for source_ids, _vectors, identifiers in staged.iter_chunks_with_names():
            start = source_offset
            stop = start + len(source_ids)
            if not np.array_equal(
                source_ids,
                np.arange(start, stop, dtype=np.int64),
            ):
                raise RuntimeError("Staged import chunk identities are inconsistent.")
            source_offset = stop
            for offset, identifier in enumerate(identifiers):
                metadata_id = None if matches is None else int(matches[start + offset])
                if metadata_id is not None and metadata_id < 0:
                    continue
                yield EmbeddingRunInputSnapshot(
                    run_id,
                    ordinal,
                    "",
                    "",
                    str(identifier),
                    Path("."),
                    MediaContentType.TEXT,
                    metadata_id,
                    None,
                    None,
                )
                ordinal += 1

    def list_import_identifier_columns(self):
        eligible = set(self.metadata.complete_unique_column_names())
        return tuple(
            (column.label, column.name)
            for column in self.metadata.list_columns()
            if column.name in eligible
        )

    def export(
        self,
        run_id: str,
        path: Path,
        *,
        metadata_column="",
        structure=None,
        dtype="preserve",
        include_provenance=True,
        compression=False,
        shard_size=0,
        cancel_check: Callable[[], bool] | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
    ):
        run = self.runs.get_run(run_id)
        if run is None or run.state != EmbeddingRunState.COMPLETED:
            raise ValueError("Select a completed embedding dataset.")
        path = Path(path)
        self._validate_export_destination(path)
        self._check_export_cancelled(cancel_check)
        matrix, names, ordinals, provenance = load_embeddings_payload_with_ids(Path(run.artifact_path))
        self._check_export_cancelled(cancel_check)
        if metadata_column:
            values = self.metadata.column_values_by_id(metadata_column)
            _known_ordinals, metadata_id_by_ordinal = (
                self.runs.input_metadata_ids_by_ordinal(run_id)
            )
            names = [
                values.get(metadata_id_by_ordinal.get(int(ordinal)), "")
                for ordinal in ordinals
            ]
        formats.validate(matrix, names, cancel_check=cancel_check)
        provenance = {**provenance, "label": run.label, "identifier_column": metadata_column or "source identifier"}
        path.parent.mkdir(parents=True, exist_ok=True)
        shard_size = max(0, int(shard_size))
        destinations = self._export_destinations(path, len(matrix), shard_size)
        if len(destinations) == 1:
            shard_size = len(matrix)
        if progress_callback is not None:
            progress_callback(0, len(matrix))
        with tempfile.TemporaryDirectory(prefix=".eidora-export-", dir=path.parent) as directory:
            staging_root = Path(directory)
            staged_members: list[tuple[Path, Path]] = []
            for index, destination in enumerate(destinations):
                self._check_export_cancelled(cancel_check)
                start = index * shard_size
                stop = min(len(matrix), start + shard_size)
                staged = staging_root / destination.name

                def report_shard_progress(completed, _total, *, offset=start):
                    if progress_callback is not None:
                        progress_callback(offset + completed, len(matrix))

                formats.write(
                    staged,
                    matrix[start:stop],
                    names[start:stop],
                    provenance=provenance,
                    structure=structure,
                    dtype=dtype,
                    include_provenance=include_provenance,
                    compression=compression,
                    cancel_check=cancel_check,
                    progress_callback=report_shard_progress,
                )
                staged_members.append((staged, destination))
            self._check_export_cancelled(cancel_check)
            self._publish_export_members(
                staging_root,
                path,
                staged_members,
            )
        if progress_callback is not None:
            progress_callback(len(matrix), len(matrix))
        return destinations[0] if len(destinations) == 1 else destinations

    def export_plan(
        self,
        run_id: str,
        path: Path,
        *,
        shard_size: int = 0,
    ) -> dict:
        run = self.runs.get_run(run_id)
        if run is None or run.state != EmbeddingRunState.COMPLETED:
            raise ValueError("Select a completed embedding dataset.")
        path = Path(path)
        self._validate_export_destination(path)
        total = self.runs.count_run_inputs(run_id)
        destinations = self._export_destinations(path, total, shard_size)
        members: list[Path] = []
        for destination in destinations:
            members.append(destination)
        existing = self._related_export_members(path)
        return {
            "destinations": destinations,
            "members": tuple(members),
            "existing": existing,
            "shard_count": len(destinations),
        }

    def list_export_identifier_columns(
        self,
        run_id: str,
        *,
        cancel_check: Callable[[], bool] | None = None,
    ):
        run = self.runs.get_run(run_id)
        if run is None or run.state != EmbeddingRunState.COMPLETED:
            return ()
        total = self.runs.count_run_inputs(run_id)
        inputs = []
        for offset in range(0, total, 5000):
            self._check_export_cancelled(cancel_check)
            inputs.extend(
                self.runs.fetch_run_inputs(run_id, limit=5000, offset=offset)
            )
        choices = []
        source_identifiers = [item.source_key.strip() for item in inputs]
        if self._is_complete_unique(source_identifiers, total):
            choices.append(("Source identifier", ""))
        for column in self.metadata.list_columns():
            self._check_export_cancelled(cancel_check)
            values_by_id = self.metadata.column_values_by_id(column.name)
            values = [
                values_by_id.get(item.metadata_row_id, "").strip()
                for item in inputs
            ]
            if self._is_complete_unique(values, total):
                choices.append((column.label, column.name))
        return tuple(choices)

    @staticmethod
    def _is_complete_unique(values, expected_count: int) -> bool:
        return (
            expected_count > 0
            and len(values) == expected_count
            and all(values)
            and len(set(values)) == expected_count
        )

    def preview_export(
        self,
        run_id: str,
        *,
        format_suffix=".parquet",
        structure=None,
        metadata_column="",
        dtype="preserve",
        include_provenance=True,
        limit=25,
        dimension_limit=50,
        cancel_check: Callable[[], bool] | None = None,
    ):
        run = self.runs.get_run(run_id)
        if run is None or run.state != EmbeddingRunState.COMPLETED:
            raise ValueError("Select a completed embedding dataset.")
        total = self.runs.count_run_inputs(run_id)
        limit = max(0, min(int(limit), 25))
        dimension_limit = max(1, min(int(dimension_limit), 50))
        run_dir = self.project.paths.embeddings / ".runs" / run_id
        object_ids = []
        vector_parts = []
        source_identifiers = []
        remaining = limit
        for chunk_ids, chunk_vectors, chunk_names in ArtifactStore(
            run_dir
        ).iter_chunks_with_names():
            self._check_export_cancelled(cancel_check)
            take = min(remaining, len(chunk_vectors))
            if take:
                object_ids.extend(int(value) for value in chunk_ids[:take])
                vector_parts.append(np.asarray(chunk_vectors[:take]))
                source_identifiers.extend(str(value) for value in chunk_names[:take])
                remaining -= take
            if remaining == 0:
                break
        if vector_parts:
            matrix = np.concatenate(vector_parts, axis=0)
        else:
            matrix, source_identifiers, artifact_ids, _provenance = (
                load_embeddings_payload_with_ids(Path(run.artifact_path))
            )
            matrix = matrix[:limit]
            source_identifiers = source_identifiers[:limit]
            object_ids = [int(value) for value in artifact_ids[:limit]]
        format_suffix = formats.ALIASES.get(format_suffix, format_suffix)
        structures = formats.FORMAT_STRUCTURES.get(format_suffix, ())
        structure = structure or (structures[0] if structures else "")
        if structure not in structures:
            raise ValueError("Choose a supported export structure.")
        if dtype != "preserve":
            matrix = matrix.astype(dtype, copy=False)
        if metadata_column:
            values = self.metadata.column_values_by_id(metadata_column)
            _known_ordinals, metadata_id_by_ordinal = (
                self.runs.input_metadata_ids_by_ordinal(run_id)
            )
            identifiers = [
                values.get(metadata_id_by_ordinal.get(ordinal), "")
                for ordinal in object_ids
            ]
        else:
            identifiers = source_identifiers
        dimensions = int(matrix.shape[1]) if matrix.ndim == 2 else 0
        formats.validate_export_options(
            format_suffix,
            identifiers,
            structure=structure,
        )
        preview_matrix = matrix[:, :dimension_limit]
        preview_columns, preview_rows = formats.preview_table(
            preview_matrix,
            identifiers,
            structure=structure,
            total_dimensions=dimensions,
        )
        item_size = matrix.dtype.itemsize if dtype == "preserve" else np.dtype(dtype).itemsize
        manifest = ArtifactStore(run_dir).read_manifest() or {}
        provenance = {
            **dict(manifest.get("provenance") or {}),
            "label": run.label,
            "identifier_column": metadata_column or "source identifier",
        }
        primary_content = formats.render_preview(
            format_suffix,
            preview_matrix,
            identifiers,
            structure=structure,
            provenance=provenance,
            include_provenance=include_provenance,
        )
        return {
            "content": primary_content,
            "primary_content": primary_content,
            "preview_kind": formats.preview_kind(format_suffix),
            "shown": len(matrix),
            "total": total,
            "dimensions": dimensions,
            "shown_dimensions": int(preview_matrix.shape[1]),
            "dtype": str(matrix.dtype),
            "estimated_bytes": total * dimensions * item_size,
            "columns": preview_columns,
            "rows": preview_rows,
            "label": run.label,
        }

    def _validate_export_destination(self, path: Path) -> None:
        resolved = path.expanduser().resolve()
        managed_root = Path(self.project.paths.embeddings).resolve()
        if resolved == managed_root or managed_root in resolved.parents:
            raise ValueError(
                "Choose an export destination outside EIDORA's managed embedding storage."
            )

    @staticmethod
    def _export_destinations(
        path: Path,
        total: int,
        shard_size: int,
    ) -> tuple[Path, ...]:
        shard_size = max(0, int(shard_size))
        if not shard_size or shard_size >= total:
            return (path,)
        shard_count = (total + shard_size - 1) // shard_size
        return tuple(
            path.with_name(
                f"{path.stem}-{index + 1:05d}-of-{shard_count:05d}{path.suffix}"
            )
            for index in range(shard_count)
        )

    @staticmethod
    def _check_export_cancelled(
        cancel_check: Callable[[], bool] | None,
    ) -> None:
        if cancel_check is not None and cancel_check():
            raise formats.ExportCancelledError("Embedding export was aborted.")

    @staticmethod
    def _related_export_members(path: Path) -> tuple[Path, ...]:
        if not path.parent.exists():
            return ()
        shard_pattern = re.compile(
            rf"^{re.escape(path.stem)}-\d{{5}}-of-\d{{5}}"
            rf"{re.escape(path.suffix)}(?:\.metadata\.json)?$"
        )
        exact_names = {path.name, path.name + ".metadata.json"}
        return tuple(
            candidate
            for candidate in path.parent.iterdir()
            if candidate.name in exact_names or shard_pattern.fullmatch(candidate.name)
        )

    def _publish_export_members(
        self,
        staging_root: Path,
        base_path: Path,
        staged_members: list[tuple[Path, Path]],
    ) -> None:
        """Publish an export set and restore the previous set if publication fails."""

        backup_root = staging_root / "previous"
        backup_root.mkdir()
        previous: list[tuple[Path, Path]] = []
        published: list[Path] = []
        try:
            for index, existing in enumerate(self._related_export_members(base_path)):
                backup = backup_root / f"{index:06d}"
                os.replace(existing, backup)
                previous.append((existing, backup))
            for staged, destination in staged_members:
                os.replace(staged, destination)
                published.append(destination)
        except Exception:
            for destination in reversed(published):
                if destination.is_dir():
                    shutil.rmtree(destination, ignore_errors=True)
                else:
                    destination.unlink(missing_ok=True)
            for original, backup in reversed(previous):
                if backup.exists():
                    os.replace(backup, original)
            raise
