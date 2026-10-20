from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3

from ProjectArea.domain import ProcessInputKind, ProcessInputReference, ProcessRecord, ProcessState


class ProcessRepository:
    """Journal unfinished thumbnail jobs and reserve their output names."""

    def __init__(self, database_path: Path) -> None:
        self._path = Path(database_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def list_records(self) -> list[ProcessRecord]:
        with closing(sqlite3.connect(self._path)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM processes ORDER BY "
                "CASE WHEN state IN ('queued','running','cancelling') THEN 0 ELSE 1 END, "
                "created_at DESC"
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def list_output_names(self) -> set[str]:
        with closing(sqlite3.connect(self._path)) as connection:
            rows = connection.execute("SELECT output_name FROM processes").fetchall()
        return {str(row[0]) for row in rows}

    def get(self, process_id: str) -> ProcessRecord | None:
        with closing(sqlite3.connect(self._path)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM processes WHERE id = ?", (process_id,)).fetchone()
        return self._from_row(row) if row is not None else None

    def add(self, record: ProcessRecord) -> None:
        with closing(sqlite3.connect(self._path)) as connection, connection:
            connection.execute(
                """INSERT INTO processes (
                    id, operation_id, operation_label, input_kind, input_id, input_label,
                    input_media_type, input_item_count, output_id, output_name, output_name_key,
                    parameters_json, state, progress, message, created_at, started_at, finished_at,
                    generated_files, failed_files, missing_files, errors_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                self._values(record),
            )

    def save(self, record: ProcessRecord) -> None:
        with closing(sqlite3.connect(self._path)) as connection, connection:
            connection.execute(
                """UPDATE processes SET state=?, progress=?, message=?, started_at=?, finished_at=?,
                    generated_files=?, failed_files=?, missing_files=?, errors_json=? WHERE id=?""",
                (
                    record.state.value, max(0, min(100, record.progress)), self._message(record.message),
                    record.started_at, record.finished_at, record.generated_files,
                    record.failed_files, record.missing_files, self._errors_json(record.errors),
                    record.id,
                ),
            )

    def output_name_exists(self, name: str) -> bool:
        key = name.strip().casefold()
        with closing(sqlite3.connect(self._path)) as connection:
            return connection.execute(
                "SELECT 1 FROM processes WHERE output_name_key = ? LIMIT 1", (key,)
            ).fetchone() is not None

    def delete(self, process_id: str) -> None:
        with closing(sqlite3.connect(self._path)) as connection, connection:
            connection.execute("DELETE FROM processes WHERE id = ?", (process_id,))

    def _ensure_schema(self) -> None:
        with closing(sqlite3.connect(self._path)) as connection, connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS processes (
                    id TEXT PRIMARY KEY, operation_id TEXT NOT NULL, operation_label TEXT NOT NULL,
                    input_kind TEXT NOT NULL, input_id TEXT NOT NULL, input_label TEXT NOT NULL,
                    input_media_type TEXT NOT NULL, input_item_count INTEGER NOT NULL,
                    output_id TEXT NOT NULL UNIQUE, output_name TEXT NOT NULL,
                    output_name_key TEXT NOT NULL UNIQUE, parameters_json TEXT NOT NULL,
                    state TEXT NOT NULL, progress INTEGER NOT NULL, message TEXT NOT NULL,
                    created_at TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT NOT NULL,
                    generated_files INTEGER NOT NULL, failed_files INTEGER NOT NULL,
                    missing_files INTEGER NOT NULL, errors_json TEXT NOT NULL
                )"""
            )

    @staticmethod
    def _values(record: ProcessRecord) -> tuple[object, ...]:
        return (
            record.id, record.operation_id, record.operation_label, record.input.kind.value,
            record.input.id, record.input.label, record.input.media_type, record.input.item_count,
            record.output_id, record.output_name, record.output_name.strip().casefold(),
            json.dumps(record.parameters, sort_keys=True), record.state.value, record.progress,
            ProcessRepository._message(record.message), record.created_at,
            record.started_at, record.finished_at,
            record.generated_files, record.failed_files, record.missing_files,
            ProcessRepository._errors_json(record.errors),
        )

    @staticmethod
    def _message(message: str) -> str:
        return str(message)[:2000]

    @staticmethod
    def _errors_json(errors: tuple[str, ...]) -> str:
        return json.dumps([str(error)[:1000] for error in errors[:10]])

    @staticmethod
    def _from_row(row: sqlite3.Row) -> ProcessRecord:
        input_ref = ProcessInputReference(
            kind=ProcessInputKind(str(row["input_kind"])), id=str(row["input_id"]),
            label=str(row["input_label"]), media_type=str(row["input_media_type"]),
            item_count=int(row["input_item_count"]),
        )
        return ProcessRecord(
            id=str(row["id"]), operation_id=str(row["operation_id"]),
            operation_label=str(row["operation_label"]), input=input_ref,
            output_id=str(row["output_id"]), output_name=str(row["output_name"]),
            parameters=dict(json.loads(row["parameters_json"] or "{}")),
            state=ProcessState(str(row["state"])), progress=int(row["progress"]),
            message=str(row["message"]), created_at=str(row["created_at"]),
            started_at=str(row["started_at"]), finished_at=str(row["finished_at"]),
            generated_files=int(row["generated_files"]), failed_files=int(row["failed_files"]),
            missing_files=int(row["missing_files"]), errors=tuple(json.loads(row["errors_json"] or "[]")),
        )
