"""Explicit interchange layouts. Imports never deserialize Python objects."""
from __future__ import annotations

import csv
from collections.abc import Callable
import io
import json
from pathlib import Path

import numpy as np

FORMATS = {
    ".parquet": "Parquet",
    ".arrow": "Arrow IPC",
    ".json": "JSON",
    ".jsonl": "JSON Lines",
    ".csv": "CSV",
    ".tsv": "TSV",
    ".h5": "HDF5",
    ".npy": "NumPy matrix",
    ".npz": "NumPy archive",
}

COMMON_EXPORT_FORMATS = tuple(FORMATS)
STRUCTURE_LABELS = {
    "matrix": "Matrix with identifiers",
    "records": "Records (identifier + embedding array)",
    "wide": "Wide table (identifier + v0, v1, …)",
}
FORMAT_STRUCTURES = {
    ".npz": ("matrix",),
    ".npy": ("matrix",),
    ".csv": ("wide", "records"),
    ".tsv": ("wide", "records"),
    ".json": ("records", "wide"),
    ".jsonl": ("records", "wide"),
    ".parquet": ("records",),
    ".arrow": ("records", "wide"),
    ".h5": ("matrix",),
}

TABULAR_PREVIEW_FORMATS = frozenset({".csv", ".tsv", ".parquet", ".arrow"})
EIDORA_PARQUET_PROFILE = "org.eidora.embeddings"
EIDORA_EMBEDDING_SCHEMA_VERSION = "1"

ALIASES = {
    ".hdf5": ".h5",
    ".hdf": ".h5",
    ".ndjson": ".jsonl",
    ".ipc": ".arrow",
    ".feather": ".arrow",
}


class ExportCancelledError(RuntimeError):
    """Raised when an embedding export is cooperatively aborted."""


def preview_kind(format_suffix: str) -> str:
    suffix = ALIASES.get(format_suffix.lower(), format_suffix.lower())
    return "table" if suffix in TABULAR_PREVIEW_FORMATS else "text"


def validate_export_options(
    format_suffix: str,
    identifiers,
    *,
    structure: str | None = None,
) -> None:
    suffix = ALIASES.get(format_suffix.lower(), format_suffix.lower())
    supported_structures = FORMAT_STRUCTURES.get(suffix)
    if not supported_structures:
        raise ValueError(f"Unsupported embedding format: {suffix}")
    selected_structure = str(structure or supported_structures[0])
    if selected_structure not in supported_structures:
        raise ValueError(
            f"{FORMATS[suffix]} does not support the {selected_structure!r} structure."
        )


def preview_table(
    matrix,
    identifiers,
    *,
    structure: str,
    total_dimensions: int,
) -> tuple[tuple[str, ...], tuple[tuple[object, ...], ...]]:
    """Return a compact, structure-aware table for the export dialog."""

    identifiers = [str(value) for value in identifiers]
    if structure == "records":
        rows = tuple(
            (
                identifier,
                json.dumps(vector.tolist())
                + (" …" if matrix.shape[1] < total_dimensions else ""),
            )
            for identifier, vector in zip(identifiers, matrix)
        )
        return ("identifier", "embeddings"), rows
    columns = ("identifier", *(f"v{index}" for index in range(matrix.shape[1])))
    rows = tuple(
        (identifier, *vector.tolist())
        for identifier, vector in zip(identifiers, matrix)
    )
    return columns, rows


def _check_cancelled(cancel_check: Callable[[], bool] | None) -> None:
    if cancel_check is not None and cancel_check():
        raise ExportCancelledError("Embedding export was aborted.")


def _report_progress(
    progress_callback: Callable[[int, int], None] | None,
    completed: int,
    total: int,
) -> None:
    if progress_callback is not None:
        progress_callback(max(0, min(completed, total)), total)


def render_preview(
    format_suffix: str,
    matrix,
    identifiers,
    *,
    structure: str | None = None,
    provenance: dict | None = None,
    include_provenance: bool = True,
) -> str:
    """Render every sampled vector in the logical structure of its output format."""
    matrix, identifiers = validate(matrix, identifiers)
    suffix = ALIASES.get(format_suffix.lower(), format_suffix.lower())
    structures = FORMAT_STRUCTURES.get(suffix, ())
    structure = str(structure or (structures[0] if structures else ""))
    identifiers = [] if identifiers is None else [str(value) for value in identifiers]
    validate_export_options(suffix, identifiers, structure=structure)
    records = [
        {
            "identifier": identifier,
            **(
                {"embeddings": vector.tolist()}
                if structure != "wide"
                else {f"v{offset}": value for offset, value in enumerate(vector.tolist())}
            ),
        }
        for identifier, vector in zip(identifiers, matrix)
    ]

    if suffix in {".csv", ".tsv"}:
        output = io.StringIO(newline="")
        writer = csv.writer(output, delimiter="\t" if suffix == ".tsv" else ",")
        if structure == "records":
            writer.writerow(["identifier", "embeddings"])
            for record in records:
                writer.writerow([record["identifier"], json.dumps(record["embeddings"])])
        else:
            writer.writerow(["identifier", *(f"v{i}" for i in range(matrix.shape[1]))])
            for identifier, vector in zip(identifiers, matrix):
                writer.writerow([identifier, *vector.tolist()])
        body = output.getvalue()
    elif suffix == ".json":
        body = json.dumps(records, ensure_ascii=False, indent=2)
    elif suffix == ".jsonl":
        body = "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n"
    else:
        array = np.array2string(
            matrix,
            threshold=matrix.size + 1,
            max_line_width=160,
            separator=", ",
        )
        identifier_array = json.dumps(identifiers, ensure_ascii=False, indent=2)
        if suffix == ".npz":
            body = (
                f"NumPy archive\n\nembeddings ({matrix.dtype}, {matrix.shape})\n{array}"
                f"\n\nidentifiers\n{identifier_array}\n\nprovenance\n"
                + json.dumps(
                    dict(provenance or {}) if include_provenance else {},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif suffix == ".npy":
            body = f"NumPy array ({matrix.dtype}, {matrix.shape})\n{array}"
        elif suffix in {".parquet", ".arrow"}:
            container = "Parquet" if suffix == ".parquet" else "Arrow IPC"
            schema = f"embeddings: fixed_size_list<item: {matrix.dtype}>[{matrix.shape[1]}]" if structure == "records" else "\n".join(
                f"v{index}: {matrix.dtype}" for index in range(matrix.shape[1])
            )
            profile = (
                f"EIDORA profile: {EIDORA_PARQUET_PROFILE} "
                f"version {EIDORA_EMBEDDING_SCHEMA_VERSION}\n"
                if suffix == ".parquet"
                else ""
            )
            body = f"{container} schema\n{profile}identifier: string\n{schema}\n\nRows\n" + "\n".join(
                json.dumps(record, ensure_ascii=False) for record in records
            )
        elif suffix == ".h5":
            body = (
                "HDF5 group /\n"
                f"├── embeddings ({matrix.dtype}, {matrix.shape})\n{array}\n"
                f"└── identifier ({len(identifiers)},)\n{identifier_array}"
            )
        else:
            raise ValueError(f"Unsupported embedding format: {suffix}")

    return body

def validate(
    matrix,
    identifiers=None,
    *,
    cancel_check: Callable[[], bool] | None = None,
):
    matrix = np.asarray(matrix)
    if matrix.ndim != 2 or not all(matrix.shape) or matrix.dtype.kind not in "fiu":
        raise ValueError("Select a nonempty two-dimensional numeric vector matrix.")
    for offset in range(0, len(matrix), 8192):
        _check_cancelled(cancel_check)
        if not np.isfinite(matrix[offset:offset + 8192]).all():
            raise ValueError("Vectors contain NaN or infinity. Repair them before importing.")
    if identifiers is not None:
        if np.asarray(identifiers).ndim != 1 or len(identifiers) != len(matrix):
            raise ValueError("Identifier count must equal vector count.")
        identifiers = [str(value) for value in identifiers]
        if any(not value.strip() for value in identifiers):
            raise ValueError("Identifiers must not be empty.")
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("Duplicate embedding identifiers are ambiguous; supply a unique mapping.")
    return matrix, identifiers


def _require_dependency(module: str):
    from importlib import import_module
    try:
        return import_module(module)
    except ImportError as exc:
        raise RuntimeError(
            f"EIDORA's required {module.split('.')[0]} dependency is unavailable."
        ) from exc


def read(path: Path, *, vector_key="embeddings", id_key="identifier", vector_columns=()):
    suffix = ALIASES.get(path.suffix.lower(), path.suffix.lower())
    identifiers, meta = None, {}
    if suffix == ".npz":
        with np.load(path, allow_pickle=False) as data:
            if vector_key not in data:
                raise ValueError(f"Vector array '{vector_key}' not found. Available arrays: {', '.join(data.files)}")
            matrix = data[vector_key]
            key = next((key for key in (id_key, "identifiers", "filenames") if key in data), None)
            identifiers = data[key].tolist() if key else None
            if "provenance" in data:
                meta = json.loads(str(data["provenance"].item()))
    elif suffix == ".npy":
        matrix = np.load(path, allow_pickle=False, mmap_mode="r")
    elif suffix in {".csv", ".tsv", ".json", ".jsonl"}:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            if suffix in {".csv", ".tsv"}:
                rows = list(csv.DictReader(handle, delimiter="\t" if suffix == ".tsv" else ","))
            elif suffix == ".jsonl":
                rows = [json.loads(line) for line in handle if line.strip()]
            else:
                rows = json.load(handle)
        if not isinstance(rows, list) or not rows:
            raise ValueError("Expected a nonempty table/list of vector records.")
        identifiers = [str(row[id_key]) for row in rows] if id_key in rows[0] else None
        default_vector_columns = tuple(
            key
            for key in rows[0]
            if key.startswith("v") and key[1:].isdigit()
        )
        columns = (
            tuple(vector_columns)
            or default_vector_columns
            or tuple(key for key in rows[0] if key != id_key)
        )
        if vector_key in rows[0]:
            matrix = [json.loads(row[vector_key]) if isinstance(row[vector_key], str) else row[vector_key] for row in rows]
        else:
            matrix = [[float(row[key]) for key in columns] for row in rows]
    elif suffix in {".parquet", ".arrow"}:
        pa = _require_dependency("pyarrow")
        if suffix == ".parquet":
            table = _require_dependency("pyarrow.parquet").read_table(path)
        else:
            with pa.memory_map(str(path), "r") as source:
                table = pa.ipc.open_file(source).read_all()
        schema_metadata = table.schema.metadata or {}
        encoded_provenance = schema_metadata.get(b"eidora.provenance")
        if encoded_provenance:
            value = json.loads(encoded_provenance.decode("utf-8"))
            if isinstance(value, dict):
                meta = value
        if vector_key in table.column_names:
            column = table[vector_key].combine_chunks()
            if pa.types.is_fixed_size_list(column.type):
                matrix = column.values.to_numpy(zero_copy_only=False).reshape(
                    len(column), column.type.list_size
                )
            else:
                matrix = column.to_pylist()
        else:
            columns = tuple(vector_columns) or tuple(
                key
                for key in table.column_names
                if key.startswith("v") and key[1:].isdigit()
            )
            if not columns:
                raise ValueError("No embedding vector columns were found.")
            matrix = np.column_stack(
                [table[column].to_numpy() for column in columns]
            )
        identifiers = table[id_key].to_pylist() if id_key in table.column_names else None
    elif suffix == ".h5":
        with _require_dependency("h5py").File(path, "r") as data:
            matrix = data[vector_key][:]
            identifiers = data[id_key].asstr()[:].tolist() if id_key in data else None
            encoded_provenance = data.attrs.get("eidora_provenance")
            if encoded_provenance:
                value = json.loads(str(encoded_provenance))
                if isinstance(value, dict):
                    meta = value
    else:
        raise ValueError(f"Unsupported embedding format: {suffix}")
    sidecar = path.with_name(path.name + ".metadata.json")
    if sidecar.exists():
        extra = json.loads(sidecar.read_text(encoding="utf-8"))
        identifiers = identifiers if identifiers is not None else extra.get("identifiers")
        meta = extra.get("provenance", meta)
    matrix, identifiers = validate(matrix, identifiers)
    return matrix, identifiers, meta


def write(
    path: Path,
    matrix,
    identifiers,
    *,
    provenance=None,
    structure=None,
    dtype="preserve",
    include_provenance=True,
    compression=False,
    cancel_check: Callable[[], bool] | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
):
    matrix, identifiers = validate(
        matrix,
        identifiers,
        cancel_check=cancel_check,
    )
    if identifiers is None:
        raise ValueError("Export requires identifiers.")
    suffix = ALIASES.get(path.suffix.lower(), path.suffix.lower())
    supported_structures = FORMAT_STRUCTURES.get(suffix)
    if not supported_structures:
        raise ValueError(f"Unsupported embedding format: {suffix}")
    structure = str(structure or supported_structures[0])
    validate_export_options(suffix, identifiers, structure=structure)
    if dtype != "preserve":
        if dtype not in {"float32", "float16"}:
            raise ValueError(f"Unsupported export data type: {dtype}")
        matrix = matrix.astype(dtype, copy=False)
    total = len(matrix)
    _check_cancelled(cancel_check)
    _report_progress(progress_callback, 0, total)

    def row_written(completed: int) -> None:
        if completed == total or completed % 256 == 0:
            _check_cancelled(cancel_check)
            _report_progress(progress_callback, completed, total)

    meta = dict(provenance or {}) if include_provenance else {}
    if suffix == ".npz":
        arrays = {
            "embeddings": matrix,
            "identifiers": np.asarray(identifiers),
            "provenance": json.dumps(meta),
        }
        writer = np.savez_compressed if compression else np.savez
        writer(path, **arrays)
    elif suffix == ".npy":
        np.save(path, matrix, allow_pickle=False)
    elif suffix in {".csv", ".tsv"}:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, delimiter="\t" if suffix == ".tsv" else ",")
            if structure == "records":
                writer.writerow(["identifier", "embeddings"])
                for index, (identifier, vector) in enumerate(zip(identifiers, matrix)):
                    writer.writerow([identifier, json.dumps(vector.tolist())])
                    row_written(index + 1)
            else:
                writer.writerow(["identifier", *(f"v{i}" for i in range(matrix.shape[1]))])
                for index, (identifier, vector) in enumerate(zip(identifiers, matrix)):
                    writer.writerow([identifier, *vector.tolist()])
                    row_written(index + 1)
    elif suffix in {".json", ".jsonl"}:
        with path.open("w", encoding="utf-8") as handle:
            if suffix == ".json":
                handle.write("[")
            for index, (identifier, vector) in enumerate(zip(identifiers, matrix)):
                if suffix == ".json" and index:
                    handle.write(",\n")
                record = {
                    "identifier": identifier,
                }
                if structure == "records":
                    record["embeddings"] = vector.tolist()
                else:
                    record.update({f"v{offset}": value for offset, value in enumerate(vector.tolist())})
                handle.write(json.dumps(record, ensure_ascii=False))
                if suffix == ".jsonl":
                    handle.write("\n")
                row_written(index + 1)
            if suffix == ".json":
                handle.write("]")
    elif suffix in {".parquet", ".arrow"}:
        pa = _require_dependency("pyarrow")
        columns = {
            "identifier": pa.array(identifiers, type=pa.string()),
        }
        if structure == "records":
            values = pa.array(
                np.ascontiguousarray(matrix).reshape(-1),
                type=pa.from_numpy_dtype(matrix.dtype),
            )
            columns["embeddings"] = pa.FixedSizeListArray.from_arrays(
                values, int(matrix.shape[1])
            )
        else:
            columns.update(
                {
                    f"v{index}": pa.array(
                        matrix[:, index], type=pa.from_numpy_dtype(matrix.dtype)
                    )
                    for index in range(matrix.shape[1])
                }
            )
        schema_metadata = {
            b"eidora.schema_version": EIDORA_EMBEDDING_SCHEMA_VERSION.encode("ascii"),
            b"eidora.structure": structure.encode("ascii"),
            b"eidora.dimensions": str(matrix.shape[1]).encode("ascii"),
        }
        if suffix == ".parquet":
            schema_metadata[b"eidora.profile"] = EIDORA_PARQUET_PROFILE.encode("ascii")
        if include_provenance:
            schema_metadata[b"eidora.provenance"] = json.dumps(
                meta, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        table = pa.table(columns).replace_schema_metadata(schema_metadata)
        if suffix == ".parquet":
            _require_dependency("pyarrow.parquet").write_table(
                table, path, compression="zstd" if compression else "none"
            )
        else:
            with pa.OSFile(str(path), "wb") as sink:
                with pa.ipc.new_file(sink, table.schema) as writer:
                    writer.write_table(table)
    elif suffix == ".h5":
        h5py = _require_dependency("h5py")
        with h5py.File(path, "w") as data:
            data.attrs["eidora_schema_version"] = EIDORA_EMBEDDING_SCHEMA_VERSION
            data.attrs["eidora_dimensions"] = int(matrix.shape[1])
            if include_provenance:
                data.attrs["eidora_provenance"] = json.dumps(meta, ensure_ascii=False)
            data.create_dataset(
                "embeddings",
                data=matrix,
                chunks=True,
                compression="gzip" if compression else None,
            )
            data.create_dataset("identifier", data=identifiers, dtype=h5py.string_dtype())
    else:
        raise ValueError(f"Unsupported embedding format: {suffix}")
    _check_cancelled(cancel_check)
    _report_progress(progress_callback, total, total)
