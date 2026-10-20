"""Release-only checks of the project chooser and saved Discovery view."""
from pathlib import Path


def choose_starter(service, project):
    from MainWindow.controllers import ProjectChooserController
    chooser = ProjectChooserController(service)
    try:
        chooser.refresh()
        chooser._select_project_path(project.root)
        chooser._open_selected_project()
        if chooser.selected_project != project:
            raise RuntimeError('Starter was not selectable in the project chooser.')
    finally:
        chooser.dispose()


def check_starter_view(controller, project):
    import numpy as np
    from PySide6 import QtCore
    from DiscoveryArea.controllers.main_controller import DiscoveryAreaController
    from DiscoveryArea.snapshot_repository import SnapshotRepository
    controller._show_area(3)
    QtCore.QCoreApplication.processEvents()
    area = next(value for value in controller._areas if isinstance(value, DiscoveryAreaController))
    repository = SnapshotRepository(project.root, project.paths.snapshot_presets)
    restored = []
    for entry in repository.list_entries():
        snapshot = repository.load(entry.path)
        if not area._restore_snapshot_from_path(entry.path):
            raise RuntimeError(f'Cannot restore starter snapshot: {entry.label}')
        QtCore.QCoreApplication.processEvents()
        expected = repository.load_coordinates(snapshot)
        np.testing.assert_allclose(area._active_dataset.coordinates, expected.coordinates)
        np.testing.assert_array_equal(area._active_dataset.metadata_ids, expected.metadata_ids)
        actual = area._build_snapshot_payload(snapshot.name)
        for name in ('appearance_state', 'filters_state', 'slicer_state'):
            expected_state = getattr(snapshot, name)
            actual_state = getattr(actual, name)
            # New controls may add defaults; every saved setting remains authoritative.
            def compare(saved, current):
                if isinstance(saved, dict):
                    return isinstance(current, dict) and all(k in current and compare(v,current[k]) for k,v in saved.items())
                return saved == current
            if not compare(expected_state, actual_state):
                raise RuntimeError(f'Starter snapshot {name} did not restore.')
        # Free frames expand to fit the recipient's window aspect ratio. Check
        # the saved center/framing, not pixel-dependent expanded world bounds.
        saved_camera, camera = snapshot.camera_state, actual.camera_state
        if saved_camera.get('viewport_aspect_preset', 'free') != camera.get('viewport_aspect_preset', 'free'):
            raise RuntimeError('Starter crop ratio did not restore.')
        spans = []
        for axis in ('x', 'y'):
            lo, hi = f'{axis}_min', f'{axis}_max'
            np.testing.assert_allclose(camera[lo] + camera[hi], saved_camera[lo] + saved_camera[hi], atol=1e-6)
            if camera[lo] > saved_camera[lo] + 1e-6 or camera[hi] < saved_camera[hi] - 1e-6:
                raise RuntimeError('Starter camera cropped the saved frame.')
            spans.append(np.isclose(camera[hi] - camera[lo], saved_camera[hi] - saved_camera[lo]))
        if not any(spans):
            raise RuntimeError('Starter camera scale did not restore.')
        for key in ('thumbnail_variant_id','thumbnail_size_px','pins_visible','areas_visible','tooltips_visible'):
            if key in snapshot.layer_state and actual.layer_state.get(key) != snapshot.layer_state[key]:
                raise RuntimeError(f'Starter layer setting did not restore: {key}')
        restored.append(snapshot.name)
    return {'snapshots_restored': restored, 'chooser_ready': True}
