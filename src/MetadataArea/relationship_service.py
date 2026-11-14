"""Conservative exact-key matching; records retain independent identities."""
from MetadataArea.relationship_repository import MetadataRelationshipRepository


def is_relationship_key(name):
    name = name.lower()
    return (name.endswith(('_id', '_key', '_filename', '_file_name'))
            or name in {'filename', 'file_name'})


class MetadataRelationshipService:
    def __init__(self, database):
        self.repository = MetadataRelationshipRepository(database)

    def preview(self, request, row_filter="all"):
        return self.repository.preview(request, row_filter)

    def best_match(self, table_id, cancelled):
        return self.repository.best_match(table_id, cancelled)

    def link(self, preview):
        return self.repository.apply(preview)
