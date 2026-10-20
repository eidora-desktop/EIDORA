# Discovery canvas

`widget.py` exposes the stable `DiscoveryCanvasWidget` API to controllers.
`scene.py` builds immutable revisioned data; `indexing.py`, `thumbnail_lod.py`,
and `thumbnail_planner.py` support selection and preview planning.
`style_preparation.py` prepares mappings; `render_backend.py` hosts Qt Quick.

Coordinates, canonical metadata IDs, visibility, and style are aligned arrays.
Render circle points in compact GPU buffers. Pan/zoom changes camera transforms,
not static point geometry. Keep loading/planning off UI and render threads.

Preview density is a fixed viewport count budget. Preserve membership during
gestures; prefetch is cache-only. Size/density edits must retain filter eligibility.
Use the [native renderer](native/README.md) for draw execution and [QML](quick/README.md)
for layer composition.

Renderer changes require both a behavior-parity check and a large-dataset
responsiveness check.
