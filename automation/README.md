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
`windows-both` to produce both formats from one build. `all` selects Windows
Store MSIX only, alongside both Mac architectures and Linux. Version tags trigger
no workflow. Store identity validation runs before dependencies are installed.
`record_unsigned_candidate.py` records each target's files, checksums, source
commit, and workflow provenance in its `eidora-candidate-<target>` upload.
See [Windows Store packaging](../packaging/windows/README.md). The MSIX remains
a submission candidate and is not included in the public GitHub release.

Application and automation regression suites are kept privately by the maintainer.
Public Source Gate validates source boundaries, UI references, resource declarations,
dependency consistency, and compilation. Packaged self-test commands remain public
because they are part of reproducible build validation.

On Ubuntu, install the packages in `packaging/linux/apt-packages.txt`, then run
`python automation/check_qt_runtime.py`. Source Gate uses that same list and
checks Qt imports and X11 plugin loading before resource validation.
`QT_QPA_PLATFORM=offscreen` does not exercise the X11 window plugin or remove
native library dependencies. The Linux bundle workflow runs its packaged
self-test under X11 through `xvfb-run`.

Use each script's `--help` for arguments. See [packaging](../packaging/README.md)
for build configuration and requirements.

Official signing and publishing are isolated in a private maintainer repository;
see [release policy](../packaging/RELEASING.md). No public job needs release secrets.
