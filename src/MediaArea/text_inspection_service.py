"""Read one saved excerpt and bounded navigation state, without its original source."""


class TextInspectionService:
    def __init__(self, repository, variant_id):
        self._repository = repository
        self._variant_id = variant_id

    def adjacent(self, metadata_id=None, *, previous=False):
        preview = self._repository.neighboring_text(self._variant_id, metadata_id, previous=previous)
        if preview is None:
            return None, False, False
        before = self._repository.neighboring_text(self._variant_id, preview.metadata_id, previous=True)
        after = self._repository.neighboring_text(self._variant_id, preview.metadata_id)
        return preview, before is not None, after is not None
