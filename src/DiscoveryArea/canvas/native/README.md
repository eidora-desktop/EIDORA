# Native renderer

`discovery_render_item.*` owns point buffers, atlas resources and drawing.
The pin and lasso overlay items own their respective scene-graph overlays.
`runtime.py` probes plugin compatibility and exposes runtime configuration.
`shaders/` contains source shaders; `qml/DiscoveryRenderer/qmldir` registers the plugin.

Build through `automation/build_native_renderer.py` from the repository root.
Use a clean build directory and a Qt SDK matching the active PySide Qt runtime.
A plugin file alone is insufficient: verify ABI loading and QML instantiation.

Create/change/delete scene-graph nodes and textures only in `updatePaintNode`.
Setters retire resources for later release; keep one texture owner per material.
Camera changes must not rebuild static world-coordinate buffers. After asynchronous
uploads, request an item redraw and a hosted-window frame.
