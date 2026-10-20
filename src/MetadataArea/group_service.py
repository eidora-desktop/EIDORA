from __future__ import annotations

import csv
from dataclasses import dataclass
import json
from pathlib import Path
import re
import tempfile
import zipfile

from MetadataArea.domain import (
    MetadataColumn,
    MetadataGroup,
    MetadataGroupColumnConflictPolicy,
    MetadataGroupExportDestination,
    MetadataGroupExportLayout,
    MetadataGroupExportPreview,
    MetadataGroupExportRequest,
    MetadataGroupExportResult,
    MetadataGroupExportStructure,
    MetadataGroupOverlapPolicy,
    MetadataGroupSet,
    MetadataGroupSetMode,
)
from MetadataArea.group_repository import MetadataGroupRepository
from MetadataArea.repository import MetadataRepository
from project.item_repository import ItemRepository


class MetadataGroupService:
    """Application behavior for metadata group sets and memberships."""

    def __init__(
        self,
        repository: MetadataGroupRepository,
        metadata_repository: MetadataRepository,
    ) -> None:
        self._repository = repository
        self._metadata_repository = metadata_repository

    def publish_exclusive_groups(self, connection, name, producer_key, assignments):
        return self._repository.publish_exclusive_groups(connection, name, producer_key, assignments)

    def list_group_sets(self) -> list[MetadataGroupSet]:
        return self._repository.list_group_sets()

    def list_groups(self, group_set_id: str | None = None) -> list[MetadataGroup]:
        return self._repository.list_groups(group_set_id)

    def list_metadata_columns(self) -> list[MetadataColumn]:
        return self._metadata_repository.list_columns()

    def group_set_member_count(self, group_set_id: str) -> int:
        return self._repository.group_set_member_count(group_set_id)

    def create_group_set(self, name: str, mode: str | MetadataGroupSetMode) -> MetadataGroupSet:
        try:
            resolved_mode = mode if isinstance(mode, MetadataGroupSetMode) else MetadataGroupSetMode(str(mode))
        except ValueError as exc:
            raise ValueError(f"Unsupported group set mode: {mode}") from exc
        return self._repository.create_group_set(name, resolved_mode)

    def create_group(self, group_set_id: str, name: str) -> MetadataGroup:
        return self._repository.create_group(group_set_id, name)

    def ensure_producer_group_set(
        self, name: str, mode: str | MetadataGroupSetMode, producer_key: str
    ) -> MetadataGroupSet:
        resolved_mode = mode if isinstance(mode, MetadataGroupSetMode) else MetadataGroupSetMode(str(mode))
        return self._repository.ensure_producer_group_set(name, resolved_mode, producer_key)

    def ensure_producer_group(
        self,
        group_set_id: str,
        name: str,
        producer_key: str,
        *,
        rename_existing: bool = False,
    ) -> MetadataGroup:
        return self._repository.ensure_producer_group(
            group_set_id, name, producer_key, rename_existing=rename_existing
        )

    def delete_producer_group(self, producer_key: str) -> None:
        self._repository.delete_producer_group(producer_key)

    def rename_group_set(self, group_set_id: str, name: str) -> None:
        self._repository.rename_group_set(group_set_id, name)

    def rename_group(self, group_id: str, name: str) -> None:
        self._repository.rename_group(group_id, name)

    def delete_group_set(self, group_set_id: str) -> None:
        self._repository.delete_group_set(group_set_id)

    def delete_group(self, group_id: str) -> None:
        self._repository.delete_group(group_id)

    def save_memberships(
        self,
        group_id: str,
        metadata_ids: list[int] | tuple[int, ...],
        operation: str = "replace",
    ) -> int:
        valid_ids = set(ItemRepository(self._metadata_repository.database_path).active_ids())
        requested = {int(value) for value in metadata_ids}
        return self._repository.apply_memberships(group_id, requested & valid_ids, operation)

    def clear(self) -> None:
        self._repository.clear()

    def preview_export(self, request: MetadataGroupExportRequest) -> MetadataGroupExportPreview:
        plan = self._prepare_export(request)
        preview_rows = tuple(
            tuple(row.get(column, "") for column in plan.output_columns)
            for row in plan.rows[:100]
        )
        return MetadataGroupExportPreview(
            valid=not plan.errors,
            messages=tuple(plan.errors),
            selected_group_count=len(plan.groups),
            membership_count=plan.membership_count,
            distinct_member_count=len(plan.memberships_by_id),
            overlap_count=plan.overlap_count,
            output_row_count=len(plan.rows),
            output_columns=plan.output_columns,
            conflicting_columns=plan.conflicting_columns,
            rows=preview_rows,
            text=self._preview_text(plan),
        )

    def export_groups(self, request: MetadataGroupExportRequest) -> MetadataGroupExportResult:
        plan = self._prepare_export(request)
        if plan.errors:
            raise ValueError("\n".join(plan.errors))
        if request.destination == MetadataGroupExportDestination.METADATA:
            self._write_export_to_metadata(plan)
            return MetadataGroupExportResult(
                destination=request.destination,
                output_row_count=len(plan.rows),
                output_columns=plan.output_columns,
            )

        output = Path(request.destination_path).expanduser().with_suffix(
            f".{request.export_format.lower()}"
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        if request.structure == MetadataGroupExportStructure.GROUP_FILES:
            self._write_group_files_archive(output, plan.archive_files)
        elif request.structure != MetadataGroupExportStructure.TABLE:
            output.write_text(
                json.dumps(plan.payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        elif request.export_format.lower() == "json":
            output.write_text(
                json.dumps(plan.rows, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        else:
            delimiter = "," if request.export_format.lower() == "csv" else "\t"
            with output.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=list(plan.output_columns),
                    delimiter=delimiter,
                )
                writer.writeheader()
                writer.writerows(plan.rows)
        return MetadataGroupExportResult(
            destination=request.destination,
            output_row_count=len(plan.rows),
            output_columns=plan.output_columns,
            output_path=output,
        )

    def _prepare_export(self, request: MetadataGroupExportRequest) -> "_MetadataGroupExportPlan":
        errors: list[str] = []
        all_groups = {group.group_id: group for group in self.list_groups()}
        selected_ids = tuple(dict.fromkeys(str(group_id) for group_id in request.group_ids))
        groups = tuple(all_groups[group_id] for group_id in selected_ids if group_id in all_groups)
        unknown_ids = [group_id for group_id in selected_ids if group_id not in all_groups]
        if not groups:
            errors.append("Select at least one group to export.")
        if unknown_ids:
            errors.append("One or more selected groups no longer exist.")

        if request.destination == MetadataGroupExportDestination.FILE:
            if not str(request.destination_path).strip():
                errors.append("Choose an export destination.")
            allowed_formats = {
                MetadataGroupExportStructure.TABLE: {"csv", "tsv", "json"},
                MetadataGroupExportStructure.GROUPS_WITH_MEMBERS: {"json"},
                MetadataGroupExportStructure.ITEMS_WITH_GROUPS: {"json"},
                MetadataGroupExportStructure.GROUP_FILES: {"zip"},
            }[request.structure]
            if str(request.export_format).lower() not in allowed_formats:
                errors.append(f"Unsupported group export format: {request.export_format}")
        elif request.destination == MetadataGroupExportDestination.METADATA:
            if request.structure != MetadataGroupExportStructure.TABLE:
                errors.append("Only table output can be written to project metadata.")
            elif request.layout == MetadataGroupExportLayout.MEMBERSHIP_TABLE:
                errors.append("A membership table cannot be written into metadata rows.")
        else:
            errors.append("Choose an export destination.")

        metadata_rows = self._metadata_repository.fetch_rows(order_by="id")
        metadata_by_id = {row.id: row for row in metadata_rows}
        metadata_column_names = [column.name for column in self._metadata_repository.list_columns()]
        if request.identifier_column not in metadata_column_names:
            errors.append(f"Unknown identifier column: {request.identifier_column}")
        selected_metadata_columns = tuple(
            dict.fromkeys(column for column in request.metadata_columns if column != "id")
        )
        unknown_metadata_columns = [
            column for column in selected_metadata_columns if column not in metadata_column_names
        ]
        if unknown_metadata_columns:
            errors.append(f"Unknown metadata column: {unknown_metadata_columns[0]}")
        if (
            request.structure == MetadataGroupExportStructure.GROUP_FILES
            and request.identifier_column in metadata_column_names
            and request.identifier_column != "id"
            and any(
                "\n" in row.values.get(request.identifier_column, "")
                or "\r" in row.values.get(request.identifier_column, "")
                for row in metadata_rows
            )
        ):
            errors.append(
                "The selected identifier contains line breaks and cannot be written "
                "as one identifier per line."
            )

        raw_memberships = self._repository.membership_rows_for_groups(selected_ids)
        valid_group_ids = {group.group_id for group in groups}
        memberships_by_id: dict[int, list[str]] = {}
        for metadata_id, group_id in raw_memberships:
            if metadata_id not in metadata_by_id or group_id not in valid_group_ids:
                continue
            memberships_by_id.setdefault(metadata_id, []).append(group_id)
        frozen_memberships = {
            metadata_id: tuple(group_ids)
            for metadata_id, group_ids in memberships_by_id.items()
        }
        overlap_count = sum(1 for group_ids in frozen_memberships.values() if len(group_ids) > 1)
        if (
            request.structure == MetadataGroupExportStructure.TABLE
            and request.layout == MetadataGroupExportLayout.CATEGORY_COLUMN
            and overlap_count
            and request.overlap_policy == MetadataGroupOverlapPolicy.ERROR
        ):
            errors.append(
                f"{overlap_count} item(s) belong to more than one selected group. "
                "Choose how to represent overlaps."
            )
        if (
            request.structure == MetadataGroupExportStructure.TABLE
            and request.layout
            in {
                MetadataGroupExportLayout.CATEGORY_COLUMN,
                MetadataGroupExportLayout.MEMBERSHIP_TABLE,
            }
        ):
            if not request.category_column_name.strip():
                errors.append("Choose a new column name.")
        if (
            request.structure == MetadataGroupExportStructure.TABLE
            and request.membership_value_style not in {"true_false", "one_zero", "label_empty"}
        ):
            errors.append("Choose a supported membership value style.")

        group_labels = self._unique_group_labels(groups)
        if request.structure != MetadataGroupExportStructure.TABLE:
            rows, output_columns, payload, archive_files = self._build_non_table_export(
                request=request,
                groups=groups,
                metadata_rows=metadata_rows,
                memberships_by_id=frozen_memberships,
            )
            return _MetadataGroupExportPlan(
                request=request,
                groups=groups,
                memberships_by_id=frozen_memberships,
                membership_count=sum(
                    len(group_ids) for group_ids in frozen_memberships.values()
                ),
                overlap_count=overlap_count,
                rows=rows,
                output_columns=output_columns,
                errors=errors,
                existing_metadata_columns=tuple(metadata_column_names),
                conflicting_columns=(),
                payload=payload,
                archive_files=archive_files,
            )
        output_names = self._output_names(request, groups, group_labels)
        existing_by_key = {column.casefold(): column for column in metadata_column_names}
        conflicting_columns = (
            tuple(
                existing_by_key[name.casefold()]
                for name in output_names.values()
                if name.casefold() in existing_by_key
            )
            if request.destination == MetadataGroupExportDestination.METADATA
            else ()
        )
        if request.destination == MetadataGroupExportDestination.METADATA:
            output_names, name_errors = self._resolve_metadata_column_names(
                output_names,
                metadata_column_names,
                request.column_conflict_policy,
            )
            errors.extend(name_errors)

        rows, output_columns = self._build_export_rows(
            request=request,
            groups=groups,
            group_labels=group_labels,
            output_names=output_names,
            metadata_rows=metadata_rows,
            memberships_by_id=frozen_memberships,
            selected_metadata_columns=selected_metadata_columns,
        )
        return _MetadataGroupExportPlan(
            request=request,
            groups=groups,
            memberships_by_id=frozen_memberships,
            membership_count=sum(len(group_ids) for group_ids in frozen_memberships.values()),
            overlap_count=overlap_count,
            rows=rows,
            output_columns=output_columns,
            errors=errors,
            existing_metadata_columns=tuple(metadata_column_names),
            conflicting_columns=conflicting_columns,
        )

    def _build_non_table_export(
        self,
        *,
        request: MetadataGroupExportRequest,
        groups: tuple[MetadataGroup, ...],
        metadata_rows,
        memberships_by_id: dict[int, tuple[str, ...]],
    ) -> tuple[
        list[dict[str, object]],
        tuple[str, ...],
        object,
        tuple[tuple[str, str], ...],
    ]:
        group_order = {group.group_id: index for index, group in enumerate(groups)}
        identifiers_by_id = {
            row.id: (
                row.id
                if request.identifier_column == "id"
                else row.values.get(request.identifier_column, "")
            )
            for row in metadata_rows
        }
        members_by_group: dict[str, list[object]] = {
            group.group_id: [] for group in groups
        }
        membership_records: list[dict[str, object]] = []
        for metadata in metadata_rows:
            group_ids = sorted(
                memberships_by_id.get(metadata.id, ()),
                key=group_order.__getitem__,
            )
            if not group_ids:
                continue
            identifier = identifiers_by_id[metadata.id]
            membership_records.append(
                {"identifier": identifier, "groups": list(group_ids)}
            )
            for group_id in group_ids:
                members_by_group[group_id].append(identifier)

        group_records = [
            {
                "id": group.group_id,
                "name": group.name,
                "members": members_by_group[group.group_id],
            }
            for group in groups
        ]
        if request.structure == MetadataGroupExportStructure.GROUPS_WITH_MEMBERS:
            return group_records, (), {"groups": group_records}, ()
        if request.structure == MetadataGroupExportStructure.ITEMS_WITH_GROUPS:
            group_directory = [
                {"id": group.group_id, "name": group.name}
                for group in groups
            ]
            return (
                membership_records,
                (),
                {"groups": group_directory, "memberships": membership_records},
                (),
            )

        file_names = self._group_export_file_names(groups)
        manifest_groups: list[dict[str, object]] = []
        preview_rows: list[dict[str, object]] = []
        archive_files: list[tuple[str, str]] = []
        for group in groups:
            members = members_by_group[group.group_id]
            file_name = f"groups/{file_names[group.group_id]}.txt"
            archive_files.append(
                (file_name, "".join(f"{identifier}\n" for identifier in members))
            )
            manifest_groups.append(
                {
                    "id": group.group_id,
                    "name": group.name,
                    "file": file_name,
                    "member_count": len(members),
                }
            )
            example = ", ".join(str(value) for value in members[:5])
            if len(members) > 5:
                example = f"{example}, …"
            preview_rows.append(
                {
                    "File": file_name,
                    "Members": len(members),
                    "Example identifiers": example,
                }
            )
        manifest = {"groups": manifest_groups}
        archive_files.insert(
            0,
            ("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"),
        )
        return (
            preview_rows,
            ("File", "Members", "Example identifiers"),
            manifest,
            tuple(archive_files),
        )

    @staticmethod
    def _group_export_file_names(groups: tuple[MetadataGroup, ...]) -> dict[str, str]:
        used: set[str] = set()
        names: dict[str, str] = {}
        for index, group in enumerate(groups, start=1):
            base = re.sub(r"[^A-Za-z0-9._-]+", "_", group.name).strip("._")
            base = base or f"group-{index}"
            candidate = base
            suffix = 2
            while candidate.casefold() in used:
                candidate = f"{base}-{suffix}"
                suffix += 1
            used.add(candidate.casefold())
            names[group.group_id] = candidate
        return names

    @staticmethod
    def _preview_text(plan: "_MetadataGroupExportPlan") -> str:
        if plan.request.structure == MetadataGroupExportStructure.GROUPS_WITH_MEMBERS:
            payload = {
                "groups": [
                    {**row, "members": row["members"][:100]}
                    for row in plan.rows[:100]
                ]
            }
        elif plan.request.structure == MetadataGroupExportStructure.ITEMS_WITH_GROUPS:
            payload = {
                "groups": plan.payload["groups"],
                "memberships": plan.rows[:100],
            }
        else:
            return ""
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @staticmethod
    def _write_group_files_archive(
        output: Path, archive_files: tuple[tuple[str, str], ...]
    ) -> None:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=f".{output.name}.",
                suffix=".tmp",
                dir=output.parent,
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
            with zipfile.ZipFile(
                temporary_path, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                for name, content in archive_files:
                    archive.writestr(name, content)
            temporary_path.replace(output)
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise

    def _build_export_rows(
        self,
        *,
        request: MetadataGroupExportRequest,
        groups: tuple[MetadataGroup, ...],
        group_labels: dict[str, str],
        output_names: dict[str, str],
        metadata_rows,
        memberships_by_id: dict[int, tuple[str, ...]],
        selected_metadata_columns: tuple[str, ...],
    ) -> tuple[list[dict[str, object]], tuple[str, ...]]:
        metadata_by_id = {row.id: row for row in metadata_rows}
        output_names = dict(output_names)
        if (
            request.include_all_metadata_rows
            or request.destination == MetadataGroupExportDestination.METADATA
        ):
            candidate_ids = [row.id for row in metadata_rows]
        else:
            candidate_ids = sorted(memberships_by_id)

        if request.destination == MetadataGroupExportDestination.METADATA:
            identifier_name = ""
            extra_columns: list[tuple[str, str]] = []
        else:
            identifier_name = (
                "metadata_id" if request.identifier_column == "id" else request.identifier_column
            )
            membership_name = request.category_column_name.strip() or "group"
            if (
                request.layout == MetadataGroupExportLayout.MEMBERSHIP_TABLE
                and identifier_name.casefold() == membership_name.casefold()
            ):
                identifier_name = f"metadata.{identifier_name}"
            used_names = {identifier_name.casefold()}
            if request.layout == MetadataGroupExportLayout.MEMBERSHIP_TABLE:
                used_names.add(membership_name.casefold())
            else:
                for key, output_name in output_names.items():
                    output_names[key] = self._unique_name(output_name, used_names)
            extra_columns = []
            for source_name in selected_metadata_columns:
                if source_name == request.identifier_column:
                    continue
                export_name = self._unique_name(
                    source_name,
                    used_names,
                    fallback_prefix="metadata.",
                )
                extra_columns.append((source_name, export_name))

        def base_row(metadata_id: int) -> dict[str, object]:
            if request.destination == MetadataGroupExportDestination.METADATA:
                return {}
            metadata = metadata_by_id[metadata_id]
            identifier = (
                metadata_id
                if request.identifier_column == "id"
                else metadata.values.get(request.identifier_column, "")
            )
            row: dict[str, object] = {identifier_name: identifier}
            row.update(
                {
                    export_name: metadata.values.get(source_name, "")
                    for source_name, export_name in extra_columns
                }
            )
            return row

        if request.layout == MetadataGroupExportLayout.MEMBERSHIP_TABLE:
            membership_name = request.category_column_name.strip() or "group"
            columns = (
                identifier_name,
                membership_name,
                *(name for _source, name in extra_columns),
            )
            rows: list[dict[str, object]] = []
            for metadata_id in candidate_ids:
                group_ids = memberships_by_id.get(metadata_id, ())
                if not group_ids and request.include_all_metadata_rows:
                    row = base_row(metadata_id)
                    row[membership_name] = request.unassigned_value
                    rows.append(self._ordered_row(row, columns))
                for group_id in group_ids:
                    row = base_row(metadata_id)
                    row[membership_name] = group_labels[group_id]
                    rows.append(self._ordered_row(row, columns))
            return rows, columns

        if request.layout == MetadataGroupExportLayout.GROUP_COLUMNS:
            group_columns = tuple(output_names[group.group_id] for group in groups)
            columns = (
                group_columns
                if request.destination == MetadataGroupExportDestination.METADATA
                else (identifier_name, *group_columns, *(name for _source, name in extra_columns))
            )
            rows = []
            for metadata_id in candidate_ids:
                memberships = set(memberships_by_id.get(metadata_id, ()))
                row = base_row(metadata_id)
                for group in groups:
                    row[output_names[group.group_id]] = self._membership_value(
                        request,
                        group_labels[group.group_id],
                        group.group_id in memberships,
                    )
                rows.append(self._ordered_row(row, columns))
            return rows, columns

        category_name = output_names["category"]
        columns = (
            (category_name,)
            if request.destination == MetadataGroupExportDestination.METADATA
            else (identifier_name, category_name, *(name for _source, name in extra_columns))
        )
        rows = []
        priority = [
            group_id
            for group_id in request.priority_group_ids
            if group_id in {group.group_id for group in groups}
        ]
        priority.extend(group.group_id for group in groups if group.group_id not in priority)
        priority_index = {group_id: index for index, group_id in enumerate(priority)}
        for metadata_id in candidate_ids:
            group_ids = list(memberships_by_id.get(metadata_id, ()))
            value = request.unassigned_value
            excluded_overlap = False
            if len(group_ids) == 1:
                value = group_labels[group_ids[0]]
            elif len(group_ids) > 1:
                if request.overlap_policy == MetadataGroupOverlapPolicy.MULTIPLE:
                    value = "; ".join(group_labels[group_id] for group_id in group_ids)
                elif request.overlap_policy == MetadataGroupOverlapPolicy.FIRST:
                    winner = min(group_ids, key=lambda group_id: priority_index[group_id])
                    value = group_labels[winner]
                elif request.overlap_policy == MetadataGroupOverlapPolicy.EXCLUDE:
                    excluded_overlap = True
                elif request.overlap_policy == MetadataGroupOverlapPolicy.ERROR:
                    value = "; ".join(group_labels[group_id] for group_id in group_ids)
            if (
                not request.include_all_metadata_rows
                and request.destination == MetadataGroupExportDestination.FILE
                and (not value or excluded_overlap)
            ):
                continue
            row = base_row(metadata_id)
            row[category_name] = value
            rows.append(self._ordered_row(row, columns))
        return rows, columns

    def _output_names(
        self,
        request: MetadataGroupExportRequest,
        groups: tuple[MetadataGroup, ...],
        group_labels: dict[str, str],
    ) -> dict[str, str]:
        if request.layout == MetadataGroupExportLayout.CATEGORY_COLUMN:
            return {"category": request.category_column_name.strip() or "group"}
        if request.layout != MetadataGroupExportLayout.GROUP_COLUMNS:
            return {}
        prefix = (
            request.metadata_column_prefix
            if request.destination == MetadataGroupExportDestination.METADATA
            else ""
        )
        used: set[str] = set()
        return {
            group.group_id: self._unique_name(f"{prefix}{group_labels[group.group_id]}", used)
            for group in groups
        }

    @staticmethod
    def _unique_group_labels(groups: tuple[MetadataGroup, ...]) -> dict[str, str]:
        used: set[str] = set()
        labels: dict[str, str] = {}
        for group in groups:
            base = group.name.strip() or "Group"
            candidate = base
            suffix = 2
            while candidate.casefold() in used:
                candidate = f"{base} ({suffix})"
                suffix += 1
            used.add(candidate.casefold())
            labels[group.group_id] = candidate
        return labels

    @staticmethod
    def _unique_name(
        requested: str,
        used: set[str],
        *,
        fallback_prefix: str = "",
    ) -> str:
        base = requested.strip() or "group"
        candidate = base
        if candidate.casefold() in used and fallback_prefix:
            candidate = f"{fallback_prefix}{base}"
        suffix = 2
        while candidate.casefold() in used:
            candidate = f"{base} ({suffix})"
            suffix += 1
        used.add(candidate.casefold())
        return candidate

    def _resolve_metadata_column_names(
        self,
        output_names: dict[str, str],
        existing_columns: list[str],
        policy: MetadataGroupColumnConflictPolicy,
    ) -> tuple[dict[str, str], list[str]]:
        resolved: dict[str, str] = {}
        errors: list[str] = []
        existing_by_key = {column.casefold(): column for column in existing_columns}
        used = set(existing_by_key)
        for key, requested in output_names.items():
            folded = requested.casefold()
            if folded not in used:
                resolved[key] = requested
                used.add(folded)
                continue
            if folded in existing_by_key and policy == MetadataGroupColumnConflictPolicy.ERROR:
                resolved[key] = requested
                errors.append(f"Metadata column already exists: {existing_by_key[folded]}")
            elif folded in existing_by_key and policy == MetadataGroupColumnConflictPolicy.REPLACE:
                resolved[key] = existing_by_key[folded]
            else:
                resolved[key] = self._unique_name(requested, used)
        return resolved, errors

    @staticmethod
    def _membership_value(
        request: MetadataGroupExportRequest,
        group_label: str,
        is_member: bool,
    ) -> str:
        if is_member:
            if request.membership_value_style == "one_zero":
                return "1"
            if request.membership_value_style == "label_empty":
                return group_label
            return "True"
        if request.unassigned_value:
            return request.unassigned_value
        if request.membership_value_style == "one_zero":
            return "0"
        if request.membership_value_style == "label_empty":
            return ""
        return "False"

    @staticmethod
    def _ordered_row(row: dict[str, object], columns: tuple[str, ...]) -> dict[str, object]:
        return {column: row.get(column, "") for column in columns}

    def _write_export_to_metadata(self, plan: "_MetadataGroupExportPlan") -> None:
        existing_by_key = {column.casefold(): column for column in plan.existing_metadata_columns}
        columns_to_add = [
            column for column in plan.output_columns if column.casefold() not in existing_by_key
        ]
        metadata_ids = self._metadata_repository.fetch_ids()
        updates = {
            metadata_id: {column: str(value) for column, value in row.items()}
            for metadata_id, row in zip(metadata_ids, plan.rows, strict=True)
        }
        self._metadata_repository.merge_add_columns(updates, columns_to_add, "")

    def export(
        self,
        destination: Path,
        export_format: str,
        *,
        group_set_id: str | None = None,
        include_metadata: bool = False,
    ) -> Path:
        output = Path(destination)
        normalized_format = str(export_format).strip().lower()
        if normalized_format not in {"csv", "tsv", "json"}:
            raise ValueError(f"Unsupported group export format: {export_format}")
        output = output.with_suffix(f".{normalized_format}")
        output.parent.mkdir(parents=True, exist_ok=True)
        rows = self._repository.membership_rows(group_set_id)
        metadata_by_id = (
            {row.id: row.values for row in self._metadata_repository.fetch_rows(order_by="id")}
            if include_metadata
            else {}
        )
        metadata_columns = (
            [column.name for column in self._metadata_repository.list_columns() if column.name != "id"]
            if include_metadata
            else []
        )
        if normalized_format == "json":
            self._write_json(output, rows, metadata_by_id, group_set_id)
            return output
        delimiter = "," if normalized_format == "csv" else "\t"
        reserved = {"metadata_id", "group_set", "group"}
        export_metadata_columns = [
            (name, f"metadata.{name}" if name in reserved else name)
            for name in metadata_columns
        ]
        with output.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "metadata_id",
                    "group_set",
                    "group",
                    *(export_name for _name, export_name in export_metadata_columns),
                ],
                delimiter=delimiter,
            )
            writer.writeheader()
            for metadata_id, set_name, group_name in rows:
                writer.writerow({
                    "metadata_id": metadata_id,
                    "group_set": set_name,
                    "group": group_name,
                    **{
                        export_name: metadata_by_id.get(metadata_id, {}).get(name, "")
                        for name, export_name in export_metadata_columns
                    },
                })
        return output

    def _write_json(
        self,
        output: Path,
        rows: list[tuple[int, str, str]],
        metadata_by_id: dict[int, dict[str, str]],
        group_set_id: str | None,
    ) -> None:
        selected_sets = [
            group_set
            for group_set in self.list_group_sets()
            if group_set_id is None or group_set.group_set_id == group_set_id
        ]
        sets: dict[str, dict[str, list[dict[str, object]]]] = {
            group_set.name: {
                group.name: [] for group in self.list_groups(group_set.group_set_id)
            }
            for group_set in selected_sets
        }
        for metadata_id, set_name, group_name in rows:
            member: dict[str, object] = {"metadata_id": metadata_id}
            if metadata_by_id:
                member["metadata"] = metadata_by_id.get(metadata_id, {})
            sets.setdefault(set_name, {}).setdefault(group_name, []).append(member)
        payload = {
            "group_sets": [
                {
                    "name": set_name,
                    "mode": next(
                        (item.mode.value for item in selected_sets if item.name == set_name),
                        "overlapping",
                    ),
                    "groups": [
                        {"name": group_name, "members": members}
                        for group_name, members in groups.items()
                    ],
                }
                for set_name, groups in sets.items()
            ]
        }
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@dataclass(frozen=True)
class _MetadataGroupExportPlan:
    request: MetadataGroupExportRequest
    groups: tuple[MetadataGroup, ...]
    memberships_by_id: dict[int, tuple[str, ...]]
    membership_count: int
    overlap_count: int
    rows: list[dict[str, object]]
    output_columns: tuple[str, ...]
    errors: list[str]
    existing_metadata_columns: tuple[str, ...]
    conflicting_columns: tuple[str, ...]
    payload: object | None = None
    archive_files: tuple[tuple[str, str], ...] = ()
