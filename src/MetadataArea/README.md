# Metadata

The Metadata UI separates the fixed **Items** tab from named source tabs. Items
is bold; source tabs are italic. Source tabs show a chain icon when they participate
in saved record links (incoming or outgoing), or a red broken chain when unlinked.
The status is also included in tab tooltips; a link does not imply every record matches. The toolbar exposes **Add files**, **Link…**,
and **Replace source…**. Manual row and column creation are not exposed.

## Ownership

- Controllers bind Designer forms and call services. Import and link previews run
  in workers and require explicit approval before publication.
- importing/service.py stages imports, checks dependencies, and publishes through
  repository.py. Source replacement preserves matched record IDs and field IDs.
- table_repository.py owns the source-table catalog and direct record links.
  relationship_repository.py owns matching previews and approved key relationships.
- resolution.py queries approved paths and returns values with source-record references.
  related_fields.py caches the shared field catalog and adapts scalar consumers.
  field_presentation.py consolidates equivalent fields and assigns unique labels.
- service.py browses, searches, edits, and exports the visible table. Exports use
  visible labels and fields, including linked values, without hidden duplicates.
- group_repository.py and group_service.py own custom groups.

## Current storage boundary

item_membership is the authoritative set of active items. Source rows have their
own internal index allocator and never enter this set. The root metadata table
remains the Items storage anchor; its publication/deletion triggers maintain membership.
Source tabs show only their own fields and never expose the internal index column.
Item numbers start at 1 and only item creation advances their counter. Source
indexes use a separate int64 range and qualified record references. Source imports,
replacement, and deletion cannot consume item numbers. Existing IDs and previously
reserved numbers remain stable; deleting an item never renumbers other items.

Vector imports against source records create or reuse items and approved record
links within the run publication transaction. Model preparation creates no items
for unlinked inputs; successful execution publishes their items and run bindings
together. Separate vector sets reuse unambiguous source or asset matches.

Projects use schema version 2. Creating an additional table in a version 1 project
backs up its metadata and manifest before upgrading. This is not the planned
schema-3 separation of items, source records, provenance, and links.

Internal IDs are immutable and never reused. An imported id column is currently
renamed to id_imported. External files are not modified by table edits.

## Imports and replacement

Add files imports into source tabs and opens the linking preview afterward. It
never fills an empty Items table. Source paths and content hashes are stored
separately from editable tab names, in the same transaction as publication. An
unchanged reimport focuses its existing tab; a changed file opens Replace source.
Different paths remain distinct, even with identical names and contents. Tab
tooltips show original paths. Older sources without recorded provenance are not
guessed from their labels.

Replace source is available only on source tabs. Matching controls are visible,
and the preview reports matched, added, and removed records. Matched records keep
their IDs and links. Removal of approved linked records, matching-key fields, or
other saved dependencies is blocked. Dependencies and preview freshness are checked
again inside publication; cancellation cannot partially publish a replacement.

The older merge_add_rows service remains in use by embedding media preparation.
Low-level column additions also remain in use by groups and Discovery computations.
These are active dependencies, not retired toolbar actions.

## Links and overview fields

Nested metadata uses the same source and Item columns. Objects become dotted
field paths; repeated objects become aligned list columns, with null entries for
missing child values. Nested collections never create extra records or Items.
The existing approved source links expose these columns in Items and Discovery.
List cells display JSON arrays and retain order, duplicates, nulls, and element
types. JSON exports retain arrays; CSV/TSV cells use JSON list notation.

SQLite stores lists as UTF-8 JSON blobs and scalars as ordinary text. The storage
class distinguishes a real list from literal text such as `[1, 2]`; existing text
is never silently decoded. `value_storage.py` owns this codec, and `values.py`
provides display and scalar-entry access. Source replacement and cell edits keep
this distinction. Editing a list requires a JSON array; `[]` clears the list.

Discovery filters test any entry, or no entry for exclusions. Conditions on
separate columns are Item-level conditions, not same-child joins. Coloring uses
one distinct non-null value; multiple distinct values remain unmapped. Numeric
list entries supply the filter domain. List columns cannot be relationship keys.

Link is disabled without an additional source tab. The dialog ranks exact matches
in both directions, including differently named columns. The best candidate is a
suggestion only, shown above a central preview. The Designer form keeps manual
table/column selectors and multiple-match approval under collapsed Link options.
No suggestion reveals those options without previewing arbitrary defaults; Find
best match reruns detection and resets multiple-match approval. Manual choices
update the preview and always win over a stale automatic result. Counts
cover all records; the preview shows at most 25 source/target value pairs.
Unmatched and multiple-match filters query the complete collection, retaining
global coverage and publication counts regardless of the selected preview filter. Existing approved pairs are not
rewritten by a key edit, and repeating approval does not duplicate relationships.

Matching is case-sensitive. Internal IDs are excluded from detection. Unique matches can be published directly. Multiple target matches require the
explicit Link all matching records option. Manual linking uses a paged record
picker, allowing several records without exposing their internal indexes.

Parent resolution follows approved paths such as item → face → photo. Duplicate
paths coalesce and cycles terminate. Cells retain every originating record. The table shows compact values, and
double-click opens a record chooser when there are multiple origins. Tooltips
show source details. Resolved values are disposable query results, never copied
into source tables. Filters match any value; negative filters match only when no
value matches. Scalar visual mappings require one distinct value, otherwise they
use the unmapped style.

Items and Discovery share plain labels. Header link icons identify inherited
fields and show source tab names on hover. Only the canonical item ID appears in
Discovery. Saved relationship-field references remain readable aliases.

Equivalent approved keys and numbered copies such as identifier_02 are consolidated
when their aligned values and coverage agree. This is a presentation rule; source
fields remain stored. Other label collisions receive numbered suffixes. Header
renaming in Items changes a unique display label, not the source schema. Source-tab
header edits remain deliberate schema edits. Changes invalidate the cached catalog.

Deletion previews dependencies and can cancel or remove them. Its recovery service
backs up databases and stages owned files for rollback. External originals remain
untouched. Saved projections and annotations protect their referenced records;
Discovery's automatic Random cache does not block replacement.

See [importing](importing/README.md) for staging and publication details and
[architecture](../../docs/architecture.md) for the remaining data-model boundaries.
