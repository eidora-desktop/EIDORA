"""Clustering records share the groups database for atomic publication."""
from __future__ import annotations
import json
from project.portable_paths import repository_paths
from MetadataArea.group_repository import MetadataGroupRepository

ACTIVE = ("running", "saving")
_RESULTS = """FROM clustering_runs AS runs
    LEFT JOIN group_sets AS groups
      ON groups.group_set_id = json_extract(runs.payload, '$.group_set_id')"""


class ClusteringRepository:
    def __init__(self, database_path, *, project_root=None):
        self._paths = repository_paths(database_path, project_root, "metadata")
        self.groups = MetadataGroupRepository(database_path)
        with self.groups.transaction() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS clustering_runs (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL, state TEXT NOT NULL,
                payload TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0)""")
            connection.execute("CREATE INDEX IF NOT EXISTS index_clustering_run ON clustering_runs(run_id)")

    def list(self, run_id=None):
        with self.groups.transaction() as connection:
            query = "SELECT runs.payload, groups.name " + _RESULTS + " WHERE runs.deleted = 0"
            rows = connection.execute(query + (" AND runs.run_id = ?" if run_id else "") + " ORDER BY runs.rowid",
                                      (run_id,) if run_id else ()).fetchall()
        return [self._record(row) for row in rows]

    def get(self, result_id):
        with self.groups.transaction() as connection:
            row = connection.execute(
                "SELECT runs.payload, groups.name " + _RESULTS + " WHERE runs.id = ? AND runs.deleted = 0",
                (result_id,),
            ).fetchone()
        if row is None:
            raise ValueError("Clustering result no longer exists.")
        return self._record(row)

    def _record(self, row):
        record = json.loads(row[0])
        if record.get("source_artifact"):
            record["source_artifact"] = self._paths.decode(record["source_artifact"])
        # Published names belong to groups, including edits made in Metadata.
        if row[1] is not None:
            record["label"] = row[1]
        return record

    def write(self, connection, record):
        record = dict(record)
        if record.get("source_artifact"):
            record["source_artifact"] = self._paths.encode(record["source_artifact"])
        connection.execute("""INSERT INTO clustering_runs(id, run_id, state, payload)
            VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET
            state=excluded.state, payload=excluded.payload""",
            (record["id"], record["run_id"], record["state"], json.dumps(record, allow_nan=False)))

    def save(self, record):
        with self.groups.transaction() as connection:
            self.write(connection, record)
        return record

    def save_unique_name(self, record):
        """Save a user-visible result name only when it is project-unique."""
        label = str(record.get("label") or "").strip()
        with self.groups.transaction() as connection:
            duplicate_result = connection.execute(
                "SELECT 1 " + _RESULTS + """
                   WHERE runs.id != ? AND runs.deleted = 0
                     AND lower(trim(coalesce(groups.name, json_extract(runs.payload, '$.label')))) = lower(?)""",
                (record["id"], label),
            ).fetchone()
            duplicate_group_set = connection.execute(
                "SELECT 1 FROM group_sets WHERE lower(trim(name)) = lower(?) AND group_set_id != ?",
                (label, str(record.get("group_set_id") or "")),
            ).fetchone()
            if duplicate_result or duplicate_group_set:
                raise ValueError(f'A clustering result named "{label}" already exists.')
            connection.execute(
                "UPDATE group_sets SET name = ? WHERE group_set_id = ?",
                (label, str(record.get("group_set_id") or "")),
            )
            self.write(connection, record)
        return record

    def enqueue(self, result_id):
        with self.groups.transaction() as connection:
            row = connection.execute(
                "SELECT payload FROM clustering_runs WHERE id=? AND deleted=0",
                (result_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Clustering result no longer exists.")
            record = json.loads(row[0])
            if record["state"] in ACTIVE or record["state"] == "completed":
                raise ValueError("This clustering result cannot be queued.")
            record.update(state="queued", error="")
            self.write(connection, record)
        return record

    def cancel_queued(self, result_id):
        with self.groups.transaction() as connection:
            row = connection.execute(
                "SELECT payload FROM clustering_runs WHERE id=? AND deleted=0",
                (result_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Clustering result no longer exists.")
            record = json.loads(row[0])
            if record["state"] != "queued":
                raise ValueError("Only queued clustering jobs can be cancelled here.")
            record.update(state="aborted", error="Clustering was cancelled before it started.")
            self.write(connection, record)
        return record

    def mark_saving(self, record):
        """Enter the commit stage without overwriting a live UI rename."""
        with self.groups.transaction() as connection:
            row = connection.execute(
                "SELECT payload FROM clustering_runs WHERE id=? AND deleted=0",
                (record["id"],),
            ).fetchone()
            if row is None:
                raise ValueError("Clustering result no longer exists.")
            latest = json.loads(row[0])
            record["label"] = latest["label"]
            record["state"] = "saving"
            self.write(connection, record)
        return record

    def start(self, result_id):
        with self.groups.transaction() as connection:
            active = connection.execute("SELECT 1 FROM clustering_runs WHERE state IN ('running', 'saving') AND deleted = 0").fetchone()
            if active:
                raise ValueError("Another clustering job is active in this project.")
            row = connection.execute("SELECT payload FROM clustering_runs WHERE id=? AND deleted=0", (result_id,)).fetchone()
            if row is None:
                raise ValueError("Clustering result no longer exists.")
            record = json.loads(row[0])
            if record["state"] == "completed":
                raise ValueError("Duplicate completed settings to create a new clustering run.")
            record.update(state="running", error="")
            self.write(connection, record)
        return record

    def recover(self):
        with self.groups.transaction() as connection:
            rows = connection.execute(
                "SELECT payload FROM clustering_runs WHERE state IN ('queued', 'running', 'saving')"
            ).fetchall()
            for row in rows:
                record = json.loads(row[0])
                record.update(state="interrupted", error="Application closed before completion. Retry starts from scratch.")
                self.write(connection, record)

    def delete(self, result_id):
        with self.groups.transaction() as connection:
            row = connection.execute("SELECT state FROM clustering_runs WHERE id=?", (result_id,)).fetchone()
            if row and row[0] in ACTIVE:
                raise ValueError("Abort the clustering run before deleting it.")
            connection.execute("DELETE FROM clustering_runs WHERE id=?", (result_id,))

    def delete_with_group_set(self, result_id, group_set_id):
        """Delete result history and its published groups in one transaction."""
        with self.groups.transaction() as connection:
            row = connection.execute(
                "SELECT state FROM clustering_runs WHERE id=? AND deleted=0",
                (result_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Clustering result no longer exists.")
            if row[0] in ACTIVE:
                raise ValueError("Abort the clustering run before deleting it.")
            if group_set_id:
                connection.execute(
                    "DELETE FROM group_sets WHERE group_set_id=?",
                    (group_set_id,),
                )
            connection.execute(
                "DELETE FROM clustering_runs WHERE id=?",
                (result_id,),
            )
