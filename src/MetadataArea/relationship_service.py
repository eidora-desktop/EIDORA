"""Conservative exact-key matching; records retain independent identities."""
from MetadataArea.relationship_repository import MetadataRelationshipRepository
from project.item_repository import ItemRepository


def is_relationship_key(name):
    name = name.lower()
    return (name.endswith(('_id', '_key', '_filename', '_file_name'))
            or name in {'filename', 'file_name'})


class MetadataRelationshipService:
    def __init__(self, database):
        self.repository = MetadataRelationshipRepository(database)

    def preview(self, request, row_filter="all"):
        self._validate_pair(request.source_table, request.target_table)
        return self.repository.preview(request, row_filter)

    def best_match(self, table_id, cancelled):
        if not self.can_link():
            return None
        return self.repository.best_match(table_id, cancelled)

    def link(self, preview):
        self._validate_pair(preview.request.source_table, preview.request.target_table)
        return self.repository.apply(preview)

    def can_link(self):
        from MetadataArea.table_repository import list_tables
        return (ItemRepository.has_items(self.repository.store.database)
                and len(list_tables(self.repository.store.database)) > 1)

    def _validate_pair(self, source, target):
        if source == target or 'metadata' not in (source, target):
            raise ValueError('Metadata can only be linked to Items.')
        if not self.can_link():
            raise ValueError('Create Items before linking metadata.')

    def link_records(self, source, ids, target, targets, name):
        self._validate_pair(source, target)
        return self.repository.store.link_records(source, ids, target, targets, name)

    def unlink(self, table_id):
        if table_id == 'metadata':
            raise ValueError('Select a metadata source tab to unlink.')
        return self.repository.store.unlink_table(table_id)

    def can_unlink(self, table_id):
        from MetadataArea.related_fields import relationships
        return table_id != 'metadata' and any(
            table_id in (source, target)
            for _, _, source, target in relationships(self.repository.store.database))
