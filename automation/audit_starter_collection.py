"""Exercise the real starter through installation, relocation and EIDORA readers."""
from contextlib import closing
import argparse
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from project.starter_collection import install_starter, read_manifest
from project.starter_validation import sha256, validate_collection
from project.service import ProjectService


def audit(manifest_path):
    from EmbeddingsArea.repository import EmbeddingRunRepository, MediaRepository
    from EmbeddingsArea.runtime.readers import load_embedding_matrix_with_ids
    from DiscoveryArea.snapshot_repository import SnapshotRepository
    from DiscoveryArea.service import DiscoveryService
    manifest=read_manifest(manifest_path)
    with tempfile.TemporaryDirectory(prefix='eidora-starter-audit-') as temp:
        workspace=Path(temp)/'workspace café'
        service=ProjectService(workspace)
        original=install_starter(service,manifest_path)
        assert original is not None
        def saved_state(root):
            return {str(path.relative_to(root)): sha256(path)
                    for directory in ('presets', 'state')
                    for path in (root/directory).rglob('*')
                    if path.is_file() and path.name != 'processes.db'}
        settings = saved_state(original.root)
        assert install_starter(service,manifest_path) is None
        assert original in service.list_recent_projects()
        moved=Path(temp)/'moved collection'
        original.root.rename(moved)
        project=service.open_project(moved)
        assert saved_state(moved) == settings, 'Saved settings changed during relocation.'
        report=validate_collection(moved,manifest['counts'])
        runs=EmbeddingRunRepository(project.paths.embeddings/'embeddings.db').list_runs()
        actual_dimensions = []
        for run in runs:
            matrix,ids,summary=load_embedding_matrix_with_ids(Path(run.artifact_path))
            dimensions = summary['dimensions']
            actual_dimensions.append(dimensions)
            assert matrix.shape == (manifest['counts']['items'],dimensions)
            if manifest['version'] >= 2:
                from EmbeddingsArea.provenance import embedding_citation_payloads
                assert embedding_citation_payloads(summary), 'Starter model citation missing.'
                assert summary['raw_dimensions'] >= dimensions
        assert sorted(actual_dimensions) == sorted(manifest['counts']['dimensions'])
        from FiguresArea.repository import FigureTabsRepository
        tabs, _ = FigureTabsRepository(moved/'state').load()
        for tab in tabs:
            composition = tab.get('composition') or {}
            for cell in composition.get('plot_cells', []):
                references = [cell.get('snapshot_path'), *cell.get('render_state', {}).get('preset_paths', {}).values()]
                for reference in references:
                    if reference:
                        assert Path(reference).is_file(), f'Figure preset is unavailable: {reference}'
        media=MediaRepository(project.paths.media/'media_index.db')
        for source in media.list_sources():
            assert source.path.is_dir()
            assert media.get_source_for_path(source.path,source.content_type).id == source.id
            assert all(asset.absolute_path.is_file() for asset in media.fetch_assets(source.id,limit=6000))
        discovery=DiscoveryService()
        snapshots=SnapshotRepository(moved,project.paths.snapshot_presets)
        for entry in snapshots.list_entries():
            snapshot=snapshots.load(entry.path)
            assert Path(snapshot.discovery_key).is_file()
            assert Path(snapshot.discovery_ref).is_file()
            frozen=snapshots.load_coordinates(snapshot)
            assert frozen.coordinates.shape == (manifest['counts']['items'],2)
        bundle=service.export_project_bundle(project,Path(temp)/'roundtrip.zip')
        imported=ProjectService(Path(temp)/'imported').import_project_bundle(bundle)
        validate_collection(imported.root,manifest['counts'])
        assert saved_state(imported.root) == settings, 'Saved settings changed during export/import.'
        assert install_starter(service,manifest_path) is None
        return report


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',type=Path,default=ROOT/'build/starter/starter-collection.json')
    args=parser.parse_args()
    print(json.dumps(audit(args.manifest),indent=2))
