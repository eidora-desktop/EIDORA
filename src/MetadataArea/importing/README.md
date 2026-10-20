# Metadata import

`readers.py` probes and streams supported formats. `domain.py` defines inspections,
column mappings, and four fixed operation plans. `service.py` stages and validates
the resulting table; `MetadataRepository` atomically publishes it. No import
request carries per-column merge actions or independent append/delete policies.

| Operation | Behavior |
| --- | --- |
| Import Metadata | Initialize an empty table with generated canonical IDs. |
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
The table toolbar uses pushbuttons without popup menus, each opening a single-file
picker. Folder readers remain available internally. Encoding, delimiter, header
row, worksheet, and record selection remain
available where applicable. Structured extraction, duplicate-column, type,
malformed-file, XML, and archive safeguards are unchanged.

The dialog keeps source-reading options in a collapsed settings panel and gives
the field-selection table the available space. Its result summary shows counts of
matched/skipped/ignored/new rows, new columns, and blocking errors without
materializing a row preview. Parsing, unique-key discovery, validation, and
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
projects. The manifest version and existing IDs are unchanged. The project item registry
reserves one ID range shared by metadata imports and independent asset analyses.

Keep this package independent of Qt. Shared legacy embedding imports/linking
remain separate compatibility entry points; Model Zoo, Vector Data, execution,
and embedding interchange retain their behavior.
