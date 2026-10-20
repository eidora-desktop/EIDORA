# Metadata

The metadata table holds descriptions keyed by permanent project item IDs.
`project/item_repository.py` owns the identity registry inside `metadata.db`;
existing metadata IDs are retained, and asset-only analyses allocate from the
same never-reused range without creating metadata rows. Its `id` is immutable; external identifiers remain ordinary columns, with incoming `id`
renamed to `id_imported`. No media path, modality, or external identifier is
mandatory. New IDs increase monotonically, including after row removal.

`controllers/main_controller.py` binds the item table and Custom Groups panel.
`service.py` owns browsing and editing; `importing/service.py` owns the four
staged import operations. `repository.py` owns table persistence and column
summaries. Group behavior and persistence use `group_service.py` and
`group_repository.py`.

Published clustering results share their display name with their group set.
Renaming from either Embeddings or Custom Groups refreshes both areas and
Discovery's virtual fields; references continue to use stable group IDs.

See [importing](importing/README.md) for exact-key operations and dependency
validation. Media sources and thumbnail preparation belong to `ProjectArea`.
Legacy `MetadataService.merge_add_rows` remains solely for existing embedding
preparation; it is not the Metadata area's Append Items workflow. Low-level
column additions remain available to groups and Discovery computations.

Replacing an unreferenced table is a clean rebuild and does not ask for matching
columns. If media links or other saved project objects reference canonical item
IDs, replacement requires exact keys to preserve those identities and blocks any
removal that would leave a dangling reference.

Batch SQLite queries below portable bind limits and explicitly close connections.

Discovery's automatic Random view is disposable and never blocks replacement.
Recognized legacy automatic random files are treated as a cache; explicit
projections, pins, marked areas, and snapshots continue to protect their items.
