# Discovery controllers

The main controller connects projections, canvas, filters, slicer, appearance,
layers, snapshots, and transforms. Panel controllers own visible controls;
coordinators own asynchronous requests and reject stale results by revision.

Call the stable `DiscoveryCanvasWidget` API rather than renderer internals.
Keep loading, indexing, and numerical work in services/computation modules.
Defer tree rebuilds and modal dialogs until the originating Qt signal returns.

Popovers above the native Quick host must be top-level transient windows; close
them when the area is hidden. Preserve filters when preview size/density changes,
and replace metadata access before reloading changed project metadata.
