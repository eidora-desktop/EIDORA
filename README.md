# EIDORA Desktop — developer guide

For information about EIDORA, visit [eidora.org](https://eidora.org).

This repository contains the desktop application's source code, Qt Designer
layouts, native Discovery renderer, and build and release automation. This guide
is for developers who want to run the source, understand its structure, and
contribute changes.

Most application code is Python with PySide6. The Discovery canvas also uses
Qt Quick, QML, and a C++ renderer. The code is organized by application area,
with explicit boundaries between UI wiring, application behavior, storage, and
computation.

For ONNX model packaging requirements, preprocessing, and the config schema,
see the [schema-v3 contract](automation/onnx_contract/ONNX_CONFIG_SCHEMA.md) and
[global preparation options](automation/onnx_contract/ONNX_PREPARATION_OPTIONS.md).
[ONNX_pipelines](https://github.com/eidora-desktop/ONNX_pipelines) validates packages
against the selected Desktop checkout and its synchronized contract snapshot.

## Set up a development environment

Clone your fork or this repository, then run the following from its root.
The project declares Python 3.11 or newer; CI uses Python 3.13.

On macOS or Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m app
```

On Windows, in PowerShell:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m app
```

The editable install makes Python source changes available without reinstalling.
It also provides the `eidora-desktop` command. Commands below assume the virtual
environment is active and the current directory is the repository root.

[pyproject.toml](pyproject.toml) declares application and development dependencies.
[packaging/runtime-requirements.txt](packaging/runtime-requirements.txt) pins the
runtime versions used by Source Gate and release builds. When investigating a
CI-only dependency problem, reproduce with those pins.

Native renderer changes require a C++ toolchain, CMake, and a Qt SDK matching the
active PySide6 Qt runtime. Follow the [native renderer guide](src/DiscoveryArea/canvas/native/README.md)
and use `python automation/build_native_renderer.py --help` for build arguments.
Keep native build directories local to your machine.

## Find the code that owns your change

Start with [src/README.md](src/README.md), then read the README in the relevant
area. `src/app.py` is the application entry point.

| Location | What belongs here |
| --- | --- |
| [MainWindow](src/MainWindow/README.md) | Application shell, navigation, and project actions |
| [ProjectArea](src/ProjectArea/README.md) | Active-project media sources and thumbnail preparation |
| [MetadataArea](src/MetadataArea/README.md) | Metadata table, import/export, and custom groups |
| [EmbeddingsArea](src/EmbeddingsArea/README.md) | Media indexing, models, embedding runs, and clustering |
| [DiscoveryArea](src/DiscoveryArea/README.md) | Projections, canvas interactions, filters, and snapshots |
| [FiguresArea](src/FiguresArea/README.md) | Figure composition, animation, and export |
| [project](src/project/README.md) | Project identity, layout, opening, and bundles |
| [application_storage](docs/storage.md) | Storage locations, verified relocation, and explicit resource cleanup |
| [shared](src/shared/README.md) | Infrastructure used across application areas |
| [automation](automation/README.md) | Source validation and build scripts |
| [packaging](packaging/README.md) | Dependency pins, resource manifests, and installer configuration |

Within an area, controllers bind widgets and call services. Services coordinate
application behavior, repositories and adapters own persistence, and computation
modules operate on plain data. Domain modules contain value objects and must
remain independent of Qt. Follow the existing boundary rather than adding
another abstraction for a small change.

Durable layouts belong in Designer `.ui` files under the area's `ui/` directory;
signal wiring belongs in `controllers/`. Use descriptive widget names and the
shared semantic icon system. Check UI changes in both light and dark themes, and
use the [shared Qt helpers](src/shared/qt/README.md) for loading forms, dialog
lifetimes, and deferred actions.

Keep expensive work off the UI thread and heavy imports out of startup. For
Discovery changes, preserve the controller-facing canvas adapter and array-based
rendering contracts. Validate interaction behavior and responsiveness with large
datasets when changing the renderer.

## Contributing

Choose one workflow or issue, reproduce its current behavior, and identify the
layer that owns it before editing. For a larger change, open an issue to describe
the intended behavior and scope before investing in the implementation.

Keep pull requests focused and easy to review. Preserve current project workflows
and include a reproducible check for changed behavior. Obsolete formats and APIs
do not require compatibility shims. For UI work, describe light/dark visual results
and the interaction steps you verified; keep screenshots local. A useful pull request
description explains the problem, the resulting behavior, and how it was tested.

Follow [CONTRIBUTING.md](CONTRIBUTING.md) and the [architecture guide](docs/architecture.md).
Before submitting, run the checks available in the public checkout:

```bash
python automation/source_gate.py
python automation/resource_inventory.py
python -m compileall -q src automation
```

Source Gate checks the public source tree, UI contracts, and resource declarations.
The resource inventory also resolves dynamically loaded dependency modules, so
it requires installed runtime dependencies. CI prints the inventory in the job
log.
Application and automation test suites are maintained privately. Include
reproducible validation steps so the maintainer can verify your change.

## Build and release changes

Read the [packaging guide](packaging/README.md) before changing dependencies,
runtime resources, or installers. Add new resources and dynamic imports to the
resource manifest, and verify new export formats through the packaged application
self-test.

The [build automation guide](automation/README.md) describes the sequence from
native renderer compilation through bundling, self-testing, and installer
creation. The workflows in [.github/workflows](.github/workflows) show the CI
environment and platform-specific build steps. Application versioning is owned
by `src/eidora_version.py`. Follow the [versioning policy](packaging/README.md#versioning)
and record release-relevant changes in [CHANGELOG.md](CHANGELOG.md).

Contributions are covered by the repository's [MIT license](LICENSE).

Official signing and publication run in a separate owner-only private repository.
Public CI creates unsigned development artifacts only. See the
[release policy](packaging/RELEASING.md) and [security reporting process](SECURITY.md).
