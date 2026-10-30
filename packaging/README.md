# Application packaging

Builds target macOS arm64 and x86_64, Windows x86_64, and Linux x86_64.
Installers are DMG, Inno Setup EXE, DEB, and AppImage respectively. Windows also
supports a separate MSIX artifact for manual Microsoft Store submission; see
[the Store release guide](windows/README.md) for identity setup, independent
Actions dropdown options, installed-package testing, and submission.

`icons/` contains the approved, size-specific platform exports from the EIDORA
design workspace. `automation/generate_app_icons.py` stages those exact files;
it must not resample a single master because the 24 px-and-smaller artwork uses
the dedicated simplified mark.
See [build automation](../automation/README.md) for the build sequence.

macOS additionally ships `icons/macos/Assets.car`, compiled from the editable
`EIDORA.icon` Icon Composer document. The teal background is the icon's full
surface. Separate SVG layers preserve the original mark and the original
three-stop teal gradient, bloom, and caustic highlights, with no inset rounded
tile. The background covers the full canvas and disables additional glass,
specular, and shadow effects; keep it behind the mark (last in the document's
front-to-back group list). Do not approximate its colors with a new gradient.
`CFBundleIconName=EIDORA` selects this layered icon on Tahoe.
The approved `.icns` remains bundled for legacy consumers; Apple also includes
flattened compatibility renditions in the catalog. Frozen Mac startup must not
override the bundle icon with `QApplication.setWindowIcon`, which replaces it
with a flattened image.

To update the modern artwork, edit `EIDORA.icon`, then run
`python automation/prepare_macos_icon.py` on a Mac with full Xcode 26 or newer
(complete Xcode's first-launch setup first). Review the default and dark
appearances in Icon Composer and the built application's Finder/Dock icon.
Commit the document, `Assets.car`, and its `catalog.json` checksum receipt
together. Packaging rejects stale artwork/catalog pairs. Release runners use
this reviewed catalog unchanged; they do not need to compile Icon Composer
documents, and Windows/Linux artwork remains independent.

- `release-targets.json` defines platform settings and bundle size limits.
- `runtime-requirements.txt` and `release-tools.txt` pin dependencies and build tools.
- `resource-manifest.toml` lists runtime resources and dynamically loaded modules.

Linux DEB `Installed-Size` describes the uncompressed installed payload, not the
download size. Count symbolic links themselves without following their targets,
round each file/link to KiB, and include payload directories, launchers, and icons.
Exclude `DEBIAN` control files, following
[Debian Policy](https://www.debian.org/doc/debian-policy/ch-controlfields.html#installed-size).

Linux packages identify Tillmann Ohm (`info@eidora.org`) as maintainer and ship
`linux/org.eidora.EIDORA.metainfo.xml` in `/usr/share/metainfo`. Both DEB and
AppImage include the metadata. This supplies developer attribution to compatible
software centers; it does not grant a verified-publisher badge. See the
[Linux distribution guide](linux/README.md) for repository trust and validation.

Bundle size limits are project-defined guards on uncompressed application files,
not platform or GitHub limits. Both macOS targets allow 700 MiB to accommodate the
pinned runtime dependencies (the Apple Silicon CI build measured 667.3 MiB).
The verified starter archive is accounted for separately, as described below.

Use a clean native build directory and a Qt SDK matching the installed PySide
runtime. Preserve platform-specific dependency pins when updating build settings.
On Windows, the bundle workflow passes native 7-Zip to aqt with `--external` to
avoid py7zr's intermittent `Bad7zFile: Specified path is bad` path-check failure
([aqtinstall #995](https://github.com/miurahr/aqtinstall/issues/995)). It resolves
the runner's `7z.exe` from PATH or Program Files and retains aqt's archive hash
verification. Windows uses `packaging/windows/aqt.ini` to run one download/extract
worker at a time: parallel native 7-Zip processes can race to create the shared
SDK directory ([aqtinstall #938](https://github.com/miurahr/aqtinstall/issues/938)).
Other platforms retain aqt's default concurrency. Qt installation logs are
included in the build diagnostics.
Keep Designer forms, semantic icons, and new dynamic dependencies in the resource
inventory. Verify new export formats with the packaged application self-test.

Video still extraction uses the BSD-licensed PyAV runtime and the FFmpeg
libraries bundled in its binary wheels. Release bundles must collect those
libraries and run the video-worker checks without relying on a system FFmpeg.
The self-test decodes six committed synthetic clips from
`src/shared/resources/video-audit/` in isolated thumbnail workers. It never
encodes video or loads PyAV in the GUI audit process. The clips contain three
96-by-64 red, green, and blue frames at 3 fps; the audit checks the green middle
frame, aspect ratio, and WebP outputs at all four requested sizes. Regenerate
them manually with `python automation/prepare_video_audit_fixtures.py` using the
pinned runtime. These clips contain no third-party media. Per-codec checkpoints
and `release-self-test.video.*.log` artifacts identify worker failures, including
native faults in windowed Windows builds.
Native-renderer probes and video-thumbnail workers return JSON through explicit
result files, because Windows windowed executables have no Python console
streams. Their callers require both a valid reply and a successful process exit.

Public workflows in `.github/workflows/` only validate source and build unsigned
development artifacts. Official signing and publication live in a separate
maintainer-only private repository; source tags do not trigger builds. Official
Windows distribution uses Store MSIX only. See [the release policy](RELEASING.md).

Build diagnostics upload on a best-effort basis so an artifact-service outage
does not prevent the installer upload from being attempted. Installer and Store
submission uploads remain required. `CreateArtifact` failures with `ENOTFOUND`
indicate a DNS lookup failure on the runner, not an invalid artifact path. Check
earlier steps for build failures, then rerun the failed job for a transient
network failure. Reruns use the original commit; to use a workflow correction,
push it and start a new manual run with the affected target (for example,
`apple-intel`).

## Versioning

EIDORA uses `MAJOR.MINOR.PATCH`, following [Semantic Versioning](https://semver.org/)
with the prototype policy below. The established prototype baseline is `0.1.0`.
Releases continue without a fixed final version. EIDORA remains a prototype;
public availability does not change this status. Release descriptions must state: **"Prototype software.
Use at your own risk. Stability and backward compatibility are not guaranteed."**
`src/eidora_version.py` remains the sole application-version source; package
metadata and release automation derive their versions from it. Only the Microsoft
Store package identity offsets the major component by one and appends `.0`
(`0.2.0` → `1.2.0.0`); app UI, reports, installer filenames, and executable
version properties retain the actual EIDORA release version.

| Release change | Example | EIDORA policy |
| --- | --- | --- |
| Patch | `0.1.0` → `0.1.1` | Compatible bug fixes, UI corrections, performance fixes, or packaging fixes without a new feature or compatibility break. |
| Minor during prototyping | `0.1.0` → `0.2.0` | New features or significant workflow additions. Breaking changes also require a minor increment during `0.x`, with compatibility and migration notes. |

Increment components rather than adding decimal numbers: a minor increment
resets patch to zero.
`0.0.1` would be an earlier version, not the next release after `0.1.0`.
There is no obligation to reach `1.0.0`, and every intermediate minor version
does not need a release. Minor components can continue beyond nine, for example
`0.9.0` → `0.10.0`, without declaring a stable release.

Prototype `0.2.0` was published for macOS and Linux on 2026-10-27 as
[`v0.2.0`](https://github.com/eidora-desktop/EIDORA/releases/tag/v0.2.0).
Linux patch `0.2.1` was published on 2026-10-27 as
[`v0.2.1`](https://github.com/eidora-desktop/EIDORA/releases/tag/v0.2.1).
Patch `0.2.2` was published for macOS and Linux on 2026-10-29 as
[`v0.2.2`](https://github.com/eidora-desktop/EIDORA/releases/tag/v0.2.2).
The next prepared patch is `0.2.3`, targeting macOS Apple Silicon and Intel,
Windows Store x64, and Linux x64. Build fresh candidates from the reviewed
0.2.3 source; do not replace published files. Record the publication date and
actual platform availability only after release review.

Prototype status does not remove release checks: verify core workflows and
packaged builds, and document known limitations and compatibility changes.
Project schema, model contract, and starter collection versions remain
independent of the application version.

For each release:

1. Keep release-relevant changes under `Unreleased` in [CHANGELOG.md](../CHANGELOG.md)
   while developing. Documentation-only edits and internal refactors do not
   require a standalone application release.
2. Compare the planned contents with the previous release and select the highest
   required increment. Check existing local and remote application tags/releases
   before assigning a version; do not infer published history from this checkout.
3. Update `src/eidora_version.py` once for the release, move the included changelog
   notes into a versioned entry, and record the actual release date when published.
   Explain any incompatibilities and migration steps explicitly.
4. Run repository verification and the packaged self-tests for the release targets.
   The release commit, installer versions, and `vMAJOR.MINOR.PATCH` tag must agree.
5. Review the draft release before publication. Never replace a published version's
   contents with changed bytes; assign a new version for a correction.

## MET starter collection

Every release includes the checksum-pinned `MET_Example-v2.zip` described by
`src/project/resources/starter-collection.json`. Build-time download uses the
`starter-met-v2` release in this repository, never a mutable latest release.
The build uses `GH_TOKEN`/`GITHUB_TOKEN` when provided for private repositories;
the installed application neither downloads the starter nor receives that token.
The archive's exact size is accounted for separately from the existing application
size limits. Corruption or a missing asset stops the build.

Prepare a new release copy from a closed, saved working project:

```sh
python automation/prepare_starter_collection.py --project /path/to/MET_Example --attribution /path/to/met_starter_dataset --output build/starter-v2 --version 2
python automation/audit_starter_collection.py --manifest build/starter-v2/starter-collection.json
```

Preparation leaves the working project unchanged, copies its linked images,
retains item identities and saved views, removes unused source-run records,
sanitizes structured paths, compacts databases, and validates all references.
Filters, slicers, appearance, camera presets, snapshots, figures, and Discovery
settings are preserved. Saved SVD transforms are checksum-validated. Use a fresh
output directory and an unused collection version; preparation refuses to
overwrite an existing release directory. When retaining historical clustering
results, pass `--clustering-note` with the reviewed provenance explanation; the
note is stored in each result and the collection README, only in the release copy.
Run it twice and compare the archive checksum when updating the dataset.
Copy the reviewed manifest into `src/project/resources/starter-collection.json`.
Do not commit the archive, workspace data, or backups into Git.

After pushing the implementation, create the **starter-met-v2** GitHub release
in `eidora-desktop/EIDORA`, leave **Set as the latest release** unchecked,
and upload these two locally prepared files:

- `build/starter-v2/MET_Example-v2.zip`
- `build/starter-v2/starter-collection.json`

The release tag deliberately does not start with `v`, so publishing the dataset
will not trigger an application version release. With an authenticated GitHub CLI,
the equivalent command is:

```sh
gh release create starter-met-v2 build/starter-v2/MET_Example-v2.zip build/starter-v2/starter-collection.json --repo eidora-desktop/EIDORA --title 'MET starter collection v2' --notes-file packaging/starter-release-notes.txt --latest=false
```

This upload is required before a clean release build can fetch the pinned asset.
Do not replace an existing version with different bytes: prepare a new version,
release tag, and manifest instead. `automation/fetch_starter_collection.py` checks
and stages the exact archive locally; `build_bundle.py` invokes it automatically.
CI also verifies it before installing the Qt SDK and compiling the renderer.
Checksum errors report both the expected and received sizes and SHA-256 hashes.
If a published asset differs, validate its contents and associated manifest before
changing the source pin; never disable verification. Remove an obsolete archive
from `build/starter` before fetching a corrected pin locally.

Packaged startup installs the starter once per workspace, including custom
`EIDORA_WORKSPACE_ROOT` locations, and opens the newly created example directly.
The welcome tour starts once the window and workspaces are ready. Later launches,
or cancelled/failed setup, show the chooser. Setup is cancellable and
keeps partial extraction hidden. A workspace receipt prevents upgrades or removal
of the user's copy from automatically installing another. Existing project names
are preserved; a collision receives the usual numeric suffix. Source-checkout
startup does not seed the development workspace. Windows, both macOS architectures,
and Linux builds run the same setup, welcome-tour, chooser, and snapshot self-test
offline. The self-test uses temporary tour preferences so validation cannot mark
the user's real onboarding as completed.
