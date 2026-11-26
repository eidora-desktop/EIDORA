# Source guide

`app.py` starts Qt and the main window. `eidora_version.py` owns the application
version. Feature areas own their workflows; keep cross-area wiring in the shell.

| Folder | Responsibility |
| --- | --- |
| [MainWindow](MainWindow/README.md) | Navigation and project actions |
| [project](project/README.md) | Project identity, layout, opening and bundles |
| [MediaArea](MediaArea/README.md) | Link media to Items and create Preview Assets |
| [MetadataArea](MetadataArea/README.md) | Metadata table, import/export and custom groups |
| [EmbeddingsArea](EmbeddingsArea/README.md) | Media indexing, models, embedding runs and clustering |
| [DiscoveryArea](DiscoveryArea/README.md) | Projections, interactive canvas, filters and snapshots |
| [FiguresArea](FiguresArea/README.md) | Figure composition, animation and export |
| [shared](shared/README.md) | Cross-area infrastructure |
| [application_info](application_info/README.md) | Release lookup and version information |

Keep dependencies explicit: controllers call services; services use repositories
and computation modules. Domain objects must not import UI or persistence code.
Repositories own durable writes; computation takes plain data, never widgets.

Keep durable layouts in Designer `.ui` files and wiring in `controllers/`.
Use descriptive widget names and semantic `iconName` properties. Verify UI
changes in light and dark themes. Keep every object name unique within a form,
preserve controller-bound names when editing, and store dynamic properties with
`stdset="0"`. Keep conditional controls visible for editing in Designer; initialize
their visibility in the controller. Validate changed forms after an open/save
round trip, including control bindings, icon metadata, and conditional behavior. Use [shared Qt helpers](shared/qt/README.md)
for loading, dialogs, lifetime handling, and deferred signal actions.

Preserve user-visible behavior and stored project compatibility. Prefer a small,
focused change over a new framework. Keep heavy imports out of startup, bound
work for large collections, and test changes at the layer that owns them.
Run the [repository checks](../README.md#contributing) before submitting changes.
