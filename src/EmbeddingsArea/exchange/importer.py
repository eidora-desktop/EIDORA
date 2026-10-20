from __future__ import annotations

from collections.abc import Callable, Iterator
import csv
import json
from pathlib import Path
import re
import shutil
import sqlite3
import uuid
import zipfile

import numpy as np

from EmbeddingsArea.exchange.domain import (
    EmbeddingImportCompanionProbe,
    EmbeddingImportInterpretation,
    EmbeddingImportProbe,
)
from EmbeddingsArea.exchange.formats import (
    ALIASES,
    EIDORA_EMBEDDING_SCHEMA_VERSION,
    EIDORA_PARQUET_PROFILE,
    FORMATS,
)


SUPPORTED_IMPORT_SUFFIXES = (
    ".parquet", ".arrow", ".json", ".jsonl", ".csv",
    ".tsv", ".h5", ".hdf5", ".npy", ".npz",
)
IMPORT_FILE_FILTER = (
    "Embedding files (*.parquet *.arrow *.json *.jsonl *.csv *.tsv *.h5 *.hdf5 *.npy *.npz);;"
    "Parquet (*.parquet);;JSON (*.json *.jsonl);;CSV / TSV (*.csv *.tsv);;"
    "HDF5 (*.h5 *.hdf5);;NumPy (*.npy *.npz);;Arrow IPC (*.arrow)"
)
IDENTIFIER_FILE_FILTER = (
    "Identifier files (*.csv *.tsv *.json *.jsonl *.parquet *.arrow *.h5 *.hdf5 *.npy *.txt);;"
    "Tabular identifiers (*.csv *.tsv *.parquet *.arrow);;JSON (*.json *.jsonl);;"
    "Array identifiers (*.npy *.h5 *.hdf5);;One identifier per line (*.txt)"
)
SIDECAR_FILE_FILTER = "JSON schema sidecar (*.json)"

_LIST_VECTOR_FIELDS = ("embeddings", "embedding", "vectors", "vector")
_IDENTIFIER_FIELDS = (
    "identifier", "identifiers", "id", "filename", "filenames",
    "source_identifier", "source_key",
)


class EmbeddingImportCancelled(RuntimeError):
    pass


def source_fingerprint(path: Path) -> tuple[int, int]:
    stat = Path(path).stat()
    return int(stat.st_size), int(stat.st_mtime_ns)


def probe_source(path: Path, *, sidecar_path: Path | None = None) -> EmbeddingImportProbe:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_IMPORT_SUFFIXES:
        raise ValueError(f"Unsupported embedding format: {suffix or 'file without an extension'}")
    if not path.is_file():
        raise ValueError("Choose a readable embedding file.")
    size = path.stat().st_size
    if size <= 0:
        raise ValueError("The selected embedding file is empty.")

    canonical = ALIASES.get(suffix, suffix)
    vectors: tuple[str, ...] = ()
    identifiers: tuple[str, ...] = ()
    wide: tuple[str, ...] = ()
    default_vector = default_identifier = ""
    row_count: int | None = None
    provenance: dict[str, object] = {}

    if canonical == ".npy":
        matrix = np.load(path, allow_pickle=False, mmap_mode="r")
        _validate_matrix_header(matrix.shape, matrix.dtype)
        row_count = int(matrix.shape[0])
        vectors, default_vector = ("embeddings",), "embeddings"
    elif canonical == ".npz":
        arrays = _npz_headers(path)
        if "embeddings" not in arrays:
            raise ValueError("NumPy archive is missing the 'embeddings' array.")
        shape, dtype = arrays["embeddings"]
        _validate_matrix_header(shape, dtype)
        row_count = int(shape[0])
        vectors, default_vector = ("embeddings",), "embeddings"
        identifiers = tuple(
            key
            for key in ("identifiers", "filenames")
            if key in arrays
            and len(arrays[key][0]) == 1
            and arrays[key][0][0] == row_count
        )
        default_identifier = identifiers[0] if identifiers else ""
        provenance = _npz_provenance(path)
    elif canonical in {".csv", ".tsv"}:
        columns, first = _csv_header(path, canonical)
        if first is None:
            raise ValueError("Expected a nonempty table with a header row.")
        vectors, wide, identifiers = _table_fields(columns)
        default_vector = vectors[0] if vectors else ""
        default_identifier = _preferred_identifier(identifiers)
    elif canonical in {".json", ".jsonl"}:
        first, envelope = _probe_json(path, lines=canonical == ".jsonl")
        if envelope is not None:
            vectors = tuple(key for key in _LIST_VECTOR_FIELDS if _is_vector_collection(envelope.get(key)))
            identifiers = tuple(key for key in _IDENTIFIER_FIELDS if _is_scalar_collection(envelope.get(key)))
            if vectors:
                row_count = len(envelope[vectors[0]])
        elif isinstance(first, dict):
            vectors, wide, identifiers = _table_fields(tuple(str(key) for key in first))
        else:
            raise ValueError("Expected JSON vector records or an object containing vector arrays.")
        default_vector = vectors[0] if vectors else ""
        default_identifier = _preferred_identifier(identifiers)
    elif canonical in {".parquet", ".arrow"}:
        columns, row_count, provenance = _arrow_info(path, canonical)
        vectors, wide, identifiers = _table_fields(columns)
        default_vector = vectors[0] if vectors else ""
        default_identifier = _preferred_identifier(identifiers)
    elif canonical == ".h5":
        datasets = _hdf5_datasets(path)
        provenance = _hdf5_provenance(path)
        vectors = tuple(
            name for name, (shape, dtype) in datasets.items()
            if len(shape) == 2 and all(shape) and np.dtype(dtype).kind in "fiu"
        )
        default_vector = "embeddings" if "embeddings" in vectors else vectors[0] if len(vectors) == 1 else ""
        if default_vector:
            row_count = datasets[default_vector][0][0]
        identifiers = tuple(
            name for name, (shape, _dtype) in datasets.items()
            if len(shape) == 1 and shape[0] > 0 and (row_count is None or shape[0] == row_count)
        )
        default_identifier = _preferred_identifier(identifiers)

    sidecar_fingerprint = None
    if sidecar_path is not None:
        sidecar_path = Path(sidecar_path)
        payload = _read_sidecar(sidecar_path)
        sidecar_fingerprint = source_fingerprint(sidecar_path)
        schema = payload.get("schema", payload)
        if not isinstance(schema, dict):
            raise ValueError("The schema sidecar must contain a JSON object.")
        requested_vector = str(schema.get("vector_key", "")).strip()
        requested_identifier = str(schema.get("identifier_key", "")).strip()
        requested_columns = schema.get("vector_columns", ())
        if requested_vector:
            vectors = tuple(dict.fromkeys((requested_vector, *vectors)))
            default_vector, wide = requested_vector, ()
        elif isinstance(requested_columns, list) and requested_columns:
            wide, default_vector = tuple(str(value) for value in requested_columns), ""
        if requested_identifier:
            identifiers = tuple(dict.fromkeys((requested_identifier, *identifiers)))
            default_identifier = requested_identifier
        if isinstance(payload.get("identifiers"), list):
            identifiers = tuple(dict.fromkeys(("<sidecar identifiers>", *identifiers)))
            if not default_identifier:
                default_identifier = "<sidecar identifiers>"
        if isinstance(payload.get("provenance"), dict):
            provenance = dict(payload["provenance"])

    if not default_vector and len(vectors) == 1:
        default_vector = vectors[0]
    if not vectors and not wide:
        raise ValueError("No vector data was detected. Add a JSON schema sidecar that names the vector field or columns.")
    return EmbeddingImportProbe(
        source_path=path, suffix=canonical, format_label=FORMATS[canonical], size_bytes=size,
        source_fingerprint=source_fingerprint(path), row_count_hint=row_count,
        vector_candidates=vectors, identifier_candidates=identifiers,
        wide_vector_columns=wide, default_vector_key=default_vector,
        default_identifier_key=default_identifier, provenance=provenance,
        sidecar_path=sidecar_path, sidecar_fingerprint=sidecar_fingerprint,
    )


def probe_identifier_source(path: Path) -> EmbeddingImportCompanionProbe:
    path = Path(path)
    suffix = path.suffix.lower()
    canonical = ALIASES.get(suffix, suffix)
    if suffix == ".txt":
        candidates, default, count = ("<values>",), "<values>", None
    elif suffix == ".npy":
        values = np.load(path, allow_pickle=False, mmap_mode="r")
        if values.ndim != 1 or not len(values):
            raise ValueError("An identifier NPY file must contain one nonempty one-dimensional array.")
        candidates, default, count = ("<values>",), "<values>", int(len(values))
    elif canonical in {".csv", ".tsv"}:
        candidates, first = _csv_header(path, canonical)
        if first is None:
            raise ValueError("The identifier file is empty.")
        count, default = None, _preferred_identifier(candidates)
        if not default and len(candidates) == 1:
            default = candidates[0]
    elif canonical in {".json", ".jsonl"}:
        first, envelope = _probe_json(path, lines=canonical == ".jsonl")
        if envelope is not None:
            candidates = tuple(str(key) for key, value in envelope.items() if _is_scalar_collection(value))
            count = len(envelope[candidates[0]]) if candidates else None
        elif isinstance(first, dict):
            candidates, count = tuple(str(key) for key in first), None
        else:
            candidates, count = ("<values>",), None
        default = _preferred_identifier(candidates)
        if not default and len(candidates) == 1:
            default = candidates[0]
    elif canonical in {".parquet", ".arrow"}:
        candidates, count = _arrow_header(path, canonical)
        default = _preferred_identifier(candidates)
        if not default and len(candidates) == 1:
            default = candidates[0]
    elif canonical == ".h5":
        datasets = _hdf5_datasets(path)
        candidates = tuple(name for name, (shape, _dtype) in datasets.items() if len(shape) == 1 and shape[0])
        count = datasets[candidates[0]][0][0] if len(candidates) == 1 else None
        default = _preferred_identifier(candidates)
        if not default and len(candidates) == 1:
            default = candidates[0]
    else:
        raise ValueError("Choose a supported identifier or index file.")
    if not path.is_file() or path.stat().st_size <= 0 or not candidates:
        raise ValueError("No identifier values were found in the selected file.")
    return EmbeddingImportCompanionProbe(
        path=path, format_label=FORMATS.get(canonical, "Text"),
        fingerprint=source_fingerprint(path), row_count_hint=count,
        identifier_candidates=candidates, default_identifier_key=default,
    )


def iter_source_chunks(
    probe: EmbeddingImportProbe,
    interpretation: EmbeddingImportInterpretation,
    temporary_path: Path,
    *, chunk_size: int = 4096,
    should_cancel: Callable[[], bool] | None = None,
) -> Iterator[tuple[np.ndarray, list[str] | None]]:
    path, suffix = probe.source_path, probe.suffix
    external = (
        iter_identifier_values(probe_identifier_source(interpretation.identifier_path), interpretation.identifier_file_key)
        if interpretation.identifier_path is not None else None
    )
    sidecar_values = None
    if interpretation.identifier_key == "<sidecar identifiers>":
        if interpretation.sidecar_path is None:
            raise ValueError("The selected schema sidecar is no longer available.")
        values = _read_sidecar(interpretation.sidecar_path).get("identifiers")
        sidecar_values = iter(str(value) for value in values) if isinstance(values, list) else None

    def cancelled() -> None:
        if should_cancel is not None and should_cancel():
            raise EmbeddingImportCancelled("Embedding import was aborted.")

    def selected_ids(raw, count: int) -> list[str] | None:
        selected = external if external is not None else sidecar_values
        if selected is None:
            return None if raw is None else list(raw)
        output = []
        for _index in range(count):
            try:
                output.append(str(next(selected)))
            except StopIteration:
                raise ValueError("Identifier count is smaller than the vector count.") from None
        return output

    def emit(matrix, raw_ids=None):
        for start in range(0, len(matrix), chunk_size):
            cancelled()
            stop = min(len(matrix), start + chunk_size)
            raw = None if raw_ids is None else raw_ids[start:stop]
            yield np.asarray(matrix[start:stop]), selected_ids(raw, stop - start)

    if suffix == ".npy":
        yield from emit(np.load(path, allow_pickle=False, mmap_mode="r"))
    elif suffix == ".npz":
        matrix_path = _extract_npz_member(
            path,
            interpretation.vector_key or "embeddings",
            temporary_path / "import_embeddings.npy",
        )
        matrix = np.load(matrix_path, allow_pickle=False, mmap_mode="r")
        raw_ids = None
        if interpretation.identifier_key:
            identifier_path = _extract_npz_member(
                path,
                interpretation.identifier_key,
                temporary_path / "import_identifiers.npy",
            )
            raw_ids = np.load(identifier_path, allow_pickle=False, mmap_mode="r")
        yield from emit(matrix, raw_ids)
    elif suffix in {".csv", ".tsv"}:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            records = csv.DictReader(handle, delimiter="\t" if suffix == ".tsv" else ",")
            yield from _emit_record_chunks(records, interpretation, selected_ids, chunk_size, cancelled)
    elif suffix in {".json", ".jsonl"}:
        if suffix == ".jsonl":
            yield from _emit_record_chunks(_iter_json_lines(path), interpretation, selected_ids, chunk_size, cancelled)
        elif _first_nonspace(path) == "[":
            yield from _emit_record_chunks(_iter_json_array(path), interpretation, selected_ids, chunk_size, cancelled)
        else:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            key = interpretation.vector_key
            if not isinstance(payload, dict) or key not in payload:
                raise ValueError(f"Vector field '{key}' was not found.")
            raw = payload.get(interpretation.identifier_key) if interpretation.identifier_key else None
            yield from emit(payload[key], raw)
    elif suffix in {".parquet", ".arrow"}:
        yield from _iter_arrow_chunks(path, suffix, interpretation, selected_ids, chunk_size, cancelled)
    elif suffix == ".h5":
        h5py = _require_dependency("h5py")
        with h5py.File(path, "r") as data:
            if not interpretation.vector_key or interpretation.vector_key not in data:
                raise ValueError("Choose the HDF5 dataset containing embedding vectors.")
            matrix = data[interpretation.vector_key]
            ids = data.get(interpretation.identifier_key) if interpretation.identifier_key else None
            for start in range(0, len(matrix), chunk_size):
                cancelled()
                stop = min(len(matrix), start + chunk_size)
                raw = None if ids is None else ids[start:stop]
                yield np.asarray(matrix[start:stop]), selected_ids(raw, stop - start)

    selected = external if external is not None else sidecar_values
    if selected is not None:
        try:
            next(selected)
        except StopIteration:
            pass
        else:
            raise ValueError("Identifier count is larger than the vector count.")


def iter_identifier_values(probe: EmbeddingImportCompanionProbe, key: str) -> Iterator[object]:
    path = probe.path
    suffix = ALIASES.get(path.suffix.lower(), path.suffix.lower())
    key = key or probe.default_identifier_key
    if not key:
        raise ValueError("Choose the identifier column or dataset from the companion file.")
    if path.suffix.lower() == ".txt":
        with path.open(encoding="utf-8-sig") as handle:
            yield from (line.rstrip("\r\n") for line in handle)
    elif suffix == ".npy":
        yield from np.load(path, allow_pickle=False, mmap_mode="r")
    elif suffix in {".csv", ".tsv"}:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle, delimiter="\t" if suffix == ".tsv" else ","):
                yield row.get(key, "")
    elif suffix == ".jsonl":
        for value in _iter_json_lines(path):
            yield value if key == "<values>" else value.get(key, "")
    elif suffix == ".json":
        if _first_nonspace(path) == "[":
            for value in _iter_json_array(path):
                yield value if key == "<values>" else value.get(key, "")
        else:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            yield from payload.get(key, [])
    elif suffix in {".parquet", ".arrow"}:
        for batch in _arrow_batches(path, suffix, 4096, [key]):
            yield from batch.column(0).to_pylist()
    elif suffix == ".h5":
        h5py = _require_dependency("h5py")
        with h5py.File(path, "r") as data:
            values = data[key]
            for start in range(0, len(values), 4096):
                yield from values[start:start + 4096]


def create_staging_directory(root: Path) -> Path:
    path = root / ".imports" / f"import_{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def identifier_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE identifiers (row_index INTEGER PRIMARY KEY, identifier TEXT NOT NULL UNIQUE)")
    return connection


def _emit_record_chunks(records, interpretation, identifiers, chunk_size, cancelled):
    vectors: list[list[float]] = []
    raw_ids: list[object] | None = [] if interpretation.identifier_key else None
    for row in records:
        cancelled()
        if not isinstance(row, dict):
            raise ValueError("Every vector record must be an object.")
        vectors.append(_table_vector(row, interpretation))
        if raw_ids is not None:
            raw_ids.append(row.get(interpretation.identifier_key, ""))
        if len(vectors) == chunk_size:
            yield np.asarray(vectors), identifiers(raw_ids, len(vectors))
            vectors, raw_ids = [], [] if interpretation.identifier_key else None
    if vectors:
        yield np.asarray(vectors), identifiers(raw_ids, len(vectors))


def _iter_arrow_chunks(path, suffix, interpretation, identifiers, chunk_size, cancelled):
    columns = list(interpretation.vector_columns)
    if interpretation.vector_key:
        columns = [interpretation.vector_key]
    if interpretation.identifier_key and interpretation.identifier_key not in columns:
        columns.append(interpretation.identifier_key)
    for batch in _arrow_batches(path, suffix, chunk_size, columns):
        cancelled()
        matrix = _arrow_vector_matrix(batch, interpretation)
        raw = (
            _arrow_column(batch, interpretation.identifier_key).to_pylist()
            if interpretation.identifier_key
            else None
        )
        yield matrix, identifiers(raw, len(matrix))


def _arrow_batches(path: Path, suffix: str, chunk_size: int, columns=None):
    pa = _require_dependency("pyarrow")
    if suffix == ".parquet":
        source = _require_dependency("pyarrow.parquet").ParquetFile(path)
        yield from source.iter_batches(batch_size=chunk_size, columns=columns)
        return
    source = pa.memory_map(str(path), "r")
    try:
        try:
            reader = pa.ipc.open_file(source)
            for index in range(reader.num_record_batches):
                batch = reader.get_batch(index)
                yield from _bounded_arrow_batches(batch, chunk_size, columns)
        except pa.ArrowInvalid:
            source.seek(0)
            for batch in pa.ipc.open_stream(source):
                yield from _bounded_arrow_batches(batch, chunk_size, columns)
    finally:
        source.close()


def _arrow_info(
    path: Path,
    suffix: str,
) -> tuple[tuple[str, ...], int | None, dict[str, object]]:
    if suffix == ".parquet":
        source = _require_dependency("pyarrow.parquet").ParquetFile(path)
        schema = source.schema_arrow
        _validate_eidora_parquet_schema(schema)
        return (
            tuple(schema.names),
            int(source.metadata.num_rows),
            _schema_provenance(schema.metadata),
        )
    pa = _require_dependency("pyarrow")
    with pa.memory_map(str(path), "r") as source:
        try:
            reader = pa.ipc.open_file(source)
            return (
                tuple(reader.schema.names),
                sum(
                    reader.get_batch(index).num_rows
                    for index in range(reader.num_record_batches)
                ),
                _schema_provenance(reader.schema.metadata),
            )
        except pa.ArrowInvalid:
            source.seek(0)
            schema = pa.ipc.open_stream(source).schema
            return tuple(schema.names), None, _schema_provenance(schema.metadata)


def _arrow_header(path: Path, suffix: str) -> tuple[tuple[str, ...], int | None]:
    columns, row_count, _provenance = _arrow_info(path, suffix)
    return columns, row_count


def _bounded_arrow_batches(batch, chunk_size: int, columns=None):
    selected = batch.select(columns) if columns else batch
    for start in range(0, selected.num_rows, chunk_size):
        yield selected.slice(start, min(chunk_size, selected.num_rows - start))


def _arrow_column(batch, name: str):
    index = batch.schema.get_field_index(name)
    if index < 0:
        raise ValueError(f"Arrow field '{name}' was not found.")
    return batch.column(index)


def _arrow_vector_matrix(batch, interpretation: EmbeddingImportInterpretation):
    pa = _require_dependency("pyarrow")
    if interpretation.vector_key:
        column = _arrow_column(batch, interpretation.vector_key)
        if pa.types.is_fixed_size_list(column.type):
            values = column.flatten().to_numpy(zero_copy_only=False)
            return np.asarray(values).reshape(
                len(column), column.type.list_size
            )
        if pa.types.is_list(column.type) or pa.types.is_large_list(column.type):
            dtype = column.flatten().to_numpy(zero_copy_only=False).dtype
            return np.asarray(column.to_pylist(), dtype=dtype)
        raise ValueError(
            f"Arrow field '{interpretation.vector_key}' is not a vector-list column."
        )
    return np.column_stack(
        [
            _arrow_column(batch, name).to_numpy(zero_copy_only=False)
            for name in interpretation.vector_columns
        ]
    )


def _schema_provenance(metadata) -> dict[str, object]:
    encoded = (metadata or {}).get(b"eidora.provenance")
    if not encoded:
        return {}
    try:
        value = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("Embedded EIDORA provenance is not valid JSON.") from None
    return dict(value) if isinstance(value, dict) else {}


def _validate_eidora_parquet_schema(schema) -> None:
    metadata = schema.metadata or {}
    encoded_profile = metadata.get(b"eidora.profile")
    if encoded_profile is None:
        return
    profile = encoded_profile.decode("utf-8", errors="replace")
    if profile != EIDORA_PARQUET_PROFILE:
        raise ValueError(f"Unsupported EIDORA Parquet profile: {profile}")
    version = metadata.get(b"eidora.schema_version", b"").decode(
        "ascii", errors="replace"
    )
    if version != EIDORA_EMBEDDING_SCHEMA_VERSION:
        raise ValueError(f"Unsupported EIDORA embedding schema version: {version or 'missing'}")
    field_index = schema.get_field_index("embeddings")
    if field_index < 0:
        raise ValueError("EIDORA Parquet file is missing the 'embeddings' field.")
    field = schema.field(field_index)
    pa = _require_dependency("pyarrow")
    if not pa.types.is_fixed_size_list(field.type):
        raise ValueError("EIDORA Parquet embeddings must be a fixed-size list.")
    identifier_index = schema.get_field_index("identifier")
    if identifier_index < 0 or not pa.types.is_string(
        schema.field(identifier_index).type
    ):
        raise ValueError("EIDORA Parquet file requires a string 'identifier' field.")
    if metadata.get(b"eidora.structure") != b"records":
        raise ValueError("EIDORA Parquet file does not use the canonical record layout.")
    encoded_dimensions = metadata.get(b"eidora.dimensions", b"")
    try:
        dimensions = int(encoded_dimensions)
    except ValueError:
        raise ValueError("EIDORA Parquet dimensions metadata is invalid.") from None
    if dimensions <= 0 or dimensions != field.type.list_size:
        raise ValueError("EIDORA Parquet dimensions do not match its vector schema.")


def _csv_header(path: Path, suffix: str):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t" if suffix == ".tsv" else ",")
        return tuple(reader.fieldnames or ()), next(reader, None)


def _table_fields(columns: tuple[str, ...]):
    vectors = tuple(name for name in _LIST_VECTOR_FIELDS if name in columns)
    wide = _wide_columns(columns)
    used = set(vectors) | set(wide)
    return vectors, wide, tuple(name for name in columns if name not in used)


def _preferred_identifier(candidates: tuple[str, ...]) -> str:
    return next((name for name in _IDENTIFIER_FIELDS if name in candidates), "")


def _wide_columns(columns: tuple[str, ...]) -> tuple[str, ...]:
    values = [name for name in columns if re.fullmatch(r"v\d+", name)]
    return tuple(sorted(values, key=lambda value: int(value[1:])))


def _table_vector(row: dict[str, object], interpretation: EmbeddingImportInterpretation) -> list[float]:
    if interpretation.vector_key:
        value = row.get(interpretation.vector_key)
        if isinstance(value, str):
            value = json.loads(value)
        if not isinstance(value, (list, tuple, np.ndarray)):
            raise ValueError(f"Vector field '{interpretation.vector_key}' is not an array.")
        return [float(item) for item in value]
    if not interpretation.vector_columns:
        raise ValueError("Choose the vector columns to import.")
    return [float(row[name]) for name in interpretation.vector_columns]


def _probe_json(path: Path, *, lines: bool):
    if lines:
        try:
            return next(_iter_json_lines(path)), None
        except StopIteration:
            raise ValueError("The selected JSON Lines file is empty.") from None
    if _first_nonspace(path) == "[":
        try:
            return next(_iter_json_array(path)), None
        except StopIteration:
            raise ValueError("The selected JSON array is empty.") from None
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON array or object.")
    return None, payload


def _iter_json_lines(path: Path):
    with path.open(encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if line.strip():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Invalid JSON on line {line_number}.") from exc


def _iter_json_array(path: Path):
    decoder = json.JSONDecoder()
    with path.open(encoding="utf-8-sig") as handle:
        buffer, started, finished = "", False, False
        while not finished:
            block = handle.read(64 * 1024)
            if block:
                buffer += block
            position = 0
            while True:
                while position < len(buffer) and buffer[position].isspace():
                    position += 1
                if not started:
                    if position >= len(buffer):
                        break
                    if buffer[position] != "[":
                        raise ValueError("Expected a JSON array.")
                    started, position = True, position + 1
                    continue
                while position < len(buffer) and (buffer[position].isspace() or buffer[position] == ","):
                    position += 1
                if position < len(buffer) and buffer[position] == "]":
                    finished, position = True, position + 1
                    break
                try:
                    value, end = decoder.raw_decode(buffer, position)
                except json.JSONDecodeError:
                    break
                yield value
                position = end
            buffer = buffer[position:]
            if not block:
                if not finished:
                    raise ValueError("The JSON array is incomplete or invalid.")
                break


def _first_nonspace(path: Path) -> str:
    with path.open(encoding="utf-8-sig") as handle:
        while char := handle.read(1):
            if not char.isspace():
                return char
    raise ValueError("The selected JSON file is empty.")


def _is_vector_collection(value) -> bool:
    return isinstance(value, list) and bool(value) and isinstance(value[0], (list, tuple))


def _is_scalar_collection(value) -> bool:
    return isinstance(value, list) and bool(value) and not isinstance(value[0], (list, dict))


def _read_sidecar(path: Path) -> dict[str, object]:
    if not Path(path).is_file():
        raise ValueError("Choose a readable JSON schema sidecar.")
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        raise ValueError("The schema sidecar is not valid JSON.") from None
    if not isinstance(payload, dict):
        raise ValueError("The schema sidecar must contain a JSON object.")
    return payload


def _npz_headers(path: Path) -> dict[str, tuple[tuple[int, ...], np.dtype]]:
    output: dict[str, tuple[tuple[int, ...], np.dtype]] = {}
    try:
        with zipfile.ZipFile(path) as archive:
            for member in archive.namelist():
                if not member.endswith(".npy") or "/" in member:
                    continue
                with archive.open(member) as handle:
                    version = np.lib.format.read_magic(handle)
                    if version == (1, 0):
                        shape, _fortran, dtype = np.lib.format.read_array_header_1_0(
                            handle
                        )
                    elif version in {(2, 0), (3, 0)}:
                        shape, _fortran, dtype = np.lib.format.read_array_header_2_0(
                            handle
                        )
                    else:
                        raise ValueError(
                            f"Unsupported NumPy array header version: {version}"
                        )
                output[Path(member).stem] = (tuple(int(value) for value in shape), dtype)
    except (OSError, zipfile.BadZipFile):
        raise ValueError("The selected NPZ file is not a readable NumPy archive.") from None
    return output


def _extract_npz_member(path: Path, key: str, destination: Path) -> Path:
    member = f"{key}.npy"
    try:
        with zipfile.ZipFile(path) as archive:
            with archive.open(member) as source, destination.open("wb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
    except KeyError:
        raise ValueError(f"NumPy archive array '{key}' was not found.") from None
    except (OSError, zipfile.BadZipFile):
        raise ValueError("The selected NPZ file is not a readable NumPy archive.") from None
    return destination


def _npz_provenance(path: Path) -> dict[str, object]:
    headers = _npz_headers(path)
    if headers.get("provenance", (None,))[0] != ():
        return {}
    try:
        with np.load(path, allow_pickle=False) as data:
            value = json.loads(str(data["provenance"].item()))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, dict) else {}


def _hdf5_datasets(path: Path):
    h5py = _require_dependency("h5py")
    output = {}
    with h5py.File(path, "r") as data:
        def visit(name, value):
            if isinstance(value, h5py.Dataset):
                output[name] = (tuple(int(size) for size in value.shape), value.dtype)
        data.visititems(visit)
    return output


def _hdf5_provenance(path: Path) -> dict[str, object]:
    h5py = _require_dependency("h5py")
    with h5py.File(path, "r") as data:
        version = data.attrs.get("eidora_schema_version")
        if version is not None and str(version) != EIDORA_EMBEDDING_SCHEMA_VERSION:
            raise ValueError(f"Unsupported EIDORA embedding schema version: {version}")
        encoded = data.attrs.get("eidora_provenance")
    if not encoded:
        return {}
    try:
        value = json.loads(str(encoded))
    except json.JSONDecodeError:
        raise ValueError("Embedded EIDORA provenance is not valid JSON.") from None
    return dict(value) if isinstance(value, dict) else {}


def _validate_matrix_header(shape, dtype) -> None:
    if len(shape) != 2 or not all(shape) or np.dtype(dtype).kind not in "fiu":
        raise ValueError("Select a nonempty two-dimensional numeric vector matrix.")


def _require_dependency(module: str):
    from importlib import import_module
    try:
        return import_module(module)
    except ImportError as exc:
        raise RuntimeError(
            f"EIDORA's required {module.split('.')[0]} dependency is unavailable."
        ) from exc
