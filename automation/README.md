# Build automation

`source_gate.py` checks source and UI contracts; `resource_inventory.py` validates
packaged resources. Run them from the repository root after installing dependencies.
Source Gate installs `packaging/runtime-requirements.txt`, matching the bundle
workflow, so the inventory can validate dynamically loaded dependency modules.
The inventory is printed in the Source Gate job log instead of uploaded as an
artifact, keeping validation independent of the artifact storage quota.

Build steps, in order:

1. `build_native_renderer.py`: compile the Qt Quick renderer.
2. `build_bundle.py`: bundle the application and dependencies.
3. `run_bundle_self_test.py`: verify the packaged application.
4. `package_installers.py`: create platform installers.
5. `package_installers.py --output-dir <installers> --checksums-only`: hash final
   installers after signing/notarization.

Windows Store builds use `package_msix.py` after the same application bundle and
self-test. Select `windows-msix` to skip EXE packaging and signing entirely, or
`windows-both` to produce both formats from one build. `all` produces both unsigned formats in the public development workflow.
Official private builds select MSIX only; version tags trigger no workflow. Store identity validation runs before dependencies are installed.
See [Windows Store packaging](../packaging/windows/README.md). Store artifacts
are uploaded separately and are not included in the public GitHub release.

Application and automation regression suites are kept privately by the maintainer.
Public Source Gate validates source boundaries, UI references, resource declarations,
dependency consistency, and compilation. Packaged self-test commands remain public
because they are part of reproducible build validation.

On a minimal Ubuntu runner, install `libegl1`, `libgl1`, `libopengl0`, and
`libxkbcommon0` before using Qt tools. `QT_QPA_PLATFORM=offscreen` removes
the display-server requirement but does not remove QtGui's native library
dependencies. Source gate installs these runtime libraries and checks the Qt
imports before resource validation.

Use each script's `--help` for arguments. See [packaging](../packaging/README.md)
for build configuration and requirements.

Official signing and publishing are isolated in a private maintainer repository;
see [release policy](../packaging/RELEASING.md). No public job needs release secrets.
