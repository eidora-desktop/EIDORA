"""Bounded navigation of generated previews, independent of the original source."""
from pathlib import Path


class ClipInspectionService:
    def __init__(self, repository, variant_id):
        self._repository = repository
        self._variant_id = variant_id

    def adjacent(self, metadata_id=-1, *, previous=False):
        return self._repository.neighboring_clip(self._variant_id, metadata_id, previous=previous)

    def paths(self, clip) -> tuple[Path, Path]:
        root = self._repository.thumbnails_root.resolve()
        paths = tuple((root / name).resolve() for name in (clip.relative_path, clip.poster_path))
        if any(not path.is_relative_to(root) for path in paths):
            raise ValueError("Preview path is outside the project preview directory.")
        return paths
