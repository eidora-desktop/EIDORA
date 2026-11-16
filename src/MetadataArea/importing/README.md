# Metadata import

`readers.py` probes and streams supported formats. `domain.py` defines inspections,
column mappings, and operation plans. `service.py` stages and validates
the resulting table; `MetadataRepository` atomically publishes it. No import
request carries per-column merge actions or independent append/delete policies.

| Operation | Behavior |
| --- | --- |
| Add files (empty table) | Initialize an empty table with generated canonical IDs. |
| Add files (merge) | Match existing keys, add unmatched rows and columns, preserve blank incoming cells, and ask how to resolve differing nonempty values. |
| Append Items | Append unmatched incoming keys, skip existing matches, preserve existing cells. |
| Update Metadata | Update selected columns for matching rows, adding columns as needed; blanks preserve existing cells; unmatched rows are counted and ignored. |
| Replace Metadata Table | Without existing item references, discard all current rows and columns and rebuild from the incoming file without matching. When project objects reference existing IDs, require exact matching, retain matched IDs, and block removal of referenced items or fields. |

Append and Update require one incoming key and one unique existing column.
Replace requires the same key pair only when existing item identities are used by
media links or other saved project objects. Keys may include canonical `id` when
appropriate. Both keys must be nonblank
and unique across their entire table. Matching strips surrounding whitespace and
uses NFC Unicode normalization; it preserves case, extensions, path syntax, and
leading zeros. Matching keys always use text interpretation. Column selection,
name mapping, and other column type/format interpretation remain available.
Initial import needs no external key. IDs are never matched by row order or reset.

Retained formats: CSV, TSV, TAB, delimited TXT, XLSX, ODS, JSON, JSONL, NDJSON,
Parquet, Arrow IPC/Feather, XML/TEI, TEI corpora, folders, and ZIP collections.
Add files opens a multi-file picker. Sources with a matching table name are
merged into that table; other sources get separate tabs. Add column and Add rows
create editable fields/records directly. Folder readers remain available internally. Encoding, delimiter, header
row, worksheet, and record selection remain
available where applicable. Structured extraction, duplicate-column, type,
malformed-file, XML, and archive safeguards are unchanged.

The Designer dialog gives the source-data preview the available space. Collapsed
Import options contain Columns, File reading, and (when required) Matching tabs.
The preview shows at most 25 sample rows with every selected column available by
horizontal scrolling; names and type labels reflect column settings, while cells
show source values. Missing matching keys reveal the Matching tab automatically. Its result summary shows counts of
matched/skipped/ignored/new rows, new columns, and blocking errors alongside a compact matching summary. Parsing, unique-key discovery, validation, and
staging run in the background. Cancellation leaves the current table intact.
Each completed validation has its own staging token so an earlier configuration
cannot publish a newer configuration's rows.

Replacement ignores the disposable automatic Random view (including recognized
legacy cache files). Explicit random analyses and saved work remain protected.
Replacement rejects removal of IDs referenced by media links, groups, embedding
inputs, Discovery projections, thumbnail variants, pins, marked areas, and saved
snapshots. `dependencies.py` additionally checks linked text-source fields,
link settings, saved pin settings, and saved Discovery field references,
including disabled filters and advanced queries. Errors identify blocking objects
and saved files. There is no destructive-reference override or reference pruning.

Source fingerprints, metadata fingerprints, staging tokens, and dependencies are
rechecked at publication, including inside the metadata transaction. The final
metadata check uses the writer's existing connection: large rollback-journal
writes can block a second reader, which must never be treated as empty or changed
metadata. Fingerprint read failures propagate instead of becoming empty-table
fingerprints. The UI
reserves the idle shared background scheduler during publication and rejects
publication when conflicting project jobs are present. Metadata, group, and media
databases are held through the transaction; rollback leaves the original table
and associations intact. Saved-file checks never construct repositories or migrate
projects. Existing IDs are unchanged. Creating additional tables upgrades the project manifest
to schema version 2 after a metadata/manifest backup. The project item registry
reserves one ID range shared by metadata imports and independent asset analyses.

Keep this package independent of Qt. Shared legacy embedding imports/linking
remain separate compatibility entry points; Model Zoo, Vector Data, execution,
and embedding interchange retain their behavior.

## Nested records and lists

JSON arrays contain records; a JSON object is one record by default. Select an
explicit record path for a wrapper collection such as `records`. Nested arrays
are never automatically promoted to record collections. JSON/JSONL and columnar
objects flatten to dotted field paths; lists of simple values retain their types
and positions. Nested arrays containing objects (including empty objects and
mixed scalar/object arrays) are rejected during inspection, with the field path
and guidance to select a record collection, restructure the file, or remove the
field. Generic XML also rejects repeated elements with child properties or
attributes; repeated scalar elements and extracted TEI header values remain
supported. Conflicting flattened paths are rejected instead of overwriting data.
Import failures, blocking validation messages, and publication-start errors appear
in the preview area without a separate popup. Correcting the settings restores
the table preview; ordinary warnings remain in the status area.
Existing saved list columns remain readable; this restriction applies to imports.

Staged row payloads retain JSON lists, including their element types and null
positions. Scalar/list mixtures promote the scalar to one entry without parsing
literal text as JSON. Empty lists remain supported. Delimited list interpretation
remains available through the column type/format controls. Publication uses the
repository's shared cell codec; previews and table display format arrays only at
the UI boundary.
