# Project storage and services

`service.py` creates, opens, renames, deletes, imports, and exports projects.
`domain.py` defines project and path value objects; `storage.py` validates the
manifest and storage layout.

Use `project.paths` rather than duplicating folder names. Each feature owns its
stores; metadata initialization belongs to `MetadataArea.repository`.
Preserve schema validation and clean up incomplete imports on failure.

`item_repository.py` owns the permanent item registry in the metadata database.
Its migration preserves existing metadata IDs and reserves a shared monotonically
increasing range. Metadata insert triggers keep legacy writers compatible;
asset-only analyses allocate IDs without creating metadata rows or media links.
The trigger's presence gates the initial backfill, so reopening a current project
does not rescan the metadata table. Publication restores it after a table swap.

`portable_paths.py` keeps internal storage references relative and resolves them
against the active project root. Repository constructors accept optional root
context and infer it for canonical EIDORA database locations. Existing absolute
external-media paths remain supported. Source IDs and item identities never
change when a portable project moves.

`starter_collection.py` owns checksum verification, safe extraction, validation,
atomic publication, and the once-per-workspace receipt. OS-owned file locks
prevent concurrent installation and survive crashes without stale lock ownership.
A publication journal recovers a completed rename if registration/receipt writing
was interrupted. UI progress and cancellation belong to the MainWindow controller;
`starter_validation.py` is read-only and imports numerical libraries only when run.
