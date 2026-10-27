# Changelog

Release-relevant changes are tracked here. Version selection follows the [versioning policy](packaging/README.md#versioning).

## Unreleased

### Fixed

- Align Discovery filter and slicer bottom insets with the existing panel spacing across native widget styles.

## 0.2.1 — Prepared, not published

This patch prepares updated Linux x64 packages with developer attribution.
It does not introduce project-format changes. Mac and Windows builds are not
part of this patch release preparation.

Prototype software. Use at your own risk. Stability and backward compatibility are not guaranteed.

### Fixed

- Identify Tillmann Ohm as the Linux package maintainer and developer, include
  the EIDORA homepage and contact address, and ship AppStream metadata in both
  DEB and AppImage packages.

## 0.2.0 — 2026-10-27

This minor prototype release adds Methods and Citations reporting, a guided
workspace tour, storage management, and platform-specific update checks.

Prototype software. Use at your own risk. Stability and backward compatibility are not guaranteed.

ONNX model packages require BibTeX; packages without it must be repackaged.
Preparation contract implementation 2 requires unfinished ONNX runs to be
prepared again; completed embedding datasets and existing saved runs remain
readable. Existing workspaces stay in place unless explicitly relocated.
Published as [v0.2.0](https://github.com/eidora-desktop/EIDORA/releases/tag/v0.2.0)
for macOS and Linux. Windows Store packaging was prepared separately.

### Added

- Add Help → Take a Tour… with a spotlight walkthrough of the five workspaces
  and their panels. Explanations sit beside red highlights, with Back, Next,
  and Finish controls. Empty workspaces are temporarily available during the
  tour, and the MET example opens it once for new users. Titles and descriptions
  can be edited in `src/MainWindow/tour_text.toml` and reload when the tour opens.
  Discovery starts with Canvas, then Projections; Figures starts with its canvas,
  followed by Snapshots and Figure Settings.
- Support optional ONNX `preparation.resize_rounding: truncate` for image and
  video models that discard fractional resize dimensions. Omitted settings keep
  the existing `round` behavior; preparation contract implementation 2 documents
  and validates both choices. Existing unfinished ONNX runs require re-preparation;
  completed embedding datasets remain unchanged.
- Add Storage controls for inspecting disk usage, copying and verifying project
  and model folders, and removing downloaded models and disposable caches.
- Show project destinations during creation and allow choosing another folder.
  New installed workspaces default to Documents/EIDORA/Projects; existing
  workspaces remain in place until explicitly relocated.
- Make MET example creation optional in the project chooser and preserve edited
  examples when creating another copy.
- Connect Check for Updates to eidora.org's platform-specific release catalog,
  including recommended prototype releases, release notes, and website downloads.
  Windows installation and updates remain managed by Microsoft Store.

### Changed

- Separate unsigned contributor builds from private official signing and publication; use MSIX for official Windows candidates, document contributor rules, and keep maintainer tests and agent instructions private.

- Show “Copied ✓” for two seconds on methods-report copy buttons after copying to the clipboard.

- Use concise, consistent menu labels for data import/export, renaming, deletion, preset and snapshot saving, and clustering removal choices.

- Shorten methods-report menu actions to “Citations…” and improve dialog readability with spaced section headings and padded tables.

- Require model BibTeX in ONNX package configs, validate it before execution, and preserve it in embedding artifacts for citations and figure captions. Older packages without config BibTeX require repackaging; saved runs remain readable.

- Unify methods, parameter settings, citations, caption text, and Markdown/BibTeX export across Discovery snapshots, Filters, Transforms, embedding runs, clustering results, and Figures snapshots; freeze new snapshot reports and offer snapshot methods in figure caption editors.

- Add paper-ready Methods and Citations reports for newly created projections, with immutable model, algorithm, parameter, version, and repeatability provenance plus Markdown and BibTeX export.
- Expand active embedding run cards onto a copyable progress report, preserve diagnostics when execution fails, and return successful runs to their Info tab.
- Present structured embedding details as expandable Info-tab child rows, including grouped execution and preprocessing settings.

### Fixed

- Correct Linux installed-size metadata by counting symbolic links themselves
  instead of counting their library targets again, and include the full payload.
- Derive Microsoft Store package versions from the EIDORA release, adding one
  only to the major component (`0.2.0` → `1.2.0.0`). Keep installer filenames
  and Windows executable version properties on the actual application version.
- Restore automatic MET example creation and opening on first packaged launch,
  followed by the welcome tour. Keep release self-tests from consuming the
  user's onboarding preference.
- Update Pillow, certifi, and filelock to patched releases for image-processing,
  certificate-trust, and file-locking vulnerabilities. Require patched minimum
  versions for source installations as well as pinned application bundles.
- Avoid the first Figures plot freezing the UI during system font discovery.
  Load Matplotlib's bundled fonts and directly locate a standard-font wishlist;
  show only available families while preserving missing saved font choices.
- Prevent the main window from blinking during project startup when Discovery
  initializes its Qt Quick widget host.
- Keep Discovery hover tooltips compact with a 360 px text-width limit, word
  wrapping for long values and identifiers, and a 300-character preview per value.
- Give the About dialog update area more room and keep its links compact.
- Retain the Qt X11 platform library and its OpenGL integration plugins in Linux
  bundles so the packaged application can create and render its window; remove
  aliases for excluded Qt libraries.
- Install and validate the complete Linux audio and X11 runtime before bundling,
  and exercise the packaged Linux application under an X11 display.
- Preserve checksum-pinned icon source bytes on Windows checkout and align the
  Linux bundle limit with its verified scientific runtime dependencies.
- Restore saved custom tooltip fields after reopening a project.
- Keep the Model Zoo install button tied to the selected model when another model's card finishes loading.
- Restore deterministic starter-settings archive preparation without modifying an existing collection release.
- Keep the Local Models Actions column wide enough to show the full Run and More button labels.
- Restrict CoreML embedding execution to native Apple Silicon on macOS; Intel Macs use CPU only, including Auto and previously saved accelerator choices.
- Match Figures snapshot row heights and label rendering to Discovery for aspect-preserving previews.
- Package a native layered macOS icon so Tahoe displays EIDORA as one teal tile instead of placing the existing icon inside a silver surround; preserve the bundle icon when the installed app starts, using the original teal gradient and highlights in both default and dark appearances.
- Exclude top-level aliases of the unused PyArrow Flight library too, avoiding dangling bundle links that fail macOS signature verification.
- Prevent failed build-diagnostics uploads from blocking installer delivery in GitHub Actions; installer and Store artifact uploads remain required.

### Documentation

- Establish application versioning rules and release tracking from the `0.1.0` prototype baseline.
- Continue releases without a fixed final version; review the next release's
  contents before choosing `0.1.1` for compatible fixes or `0.2.0` for new
  features or breaking changes.

## 0.1.0 — Prototype baseline

- Current prototype baseline confirmed by the maintainer. This entry records the starting point for release tracking, not a verified publication date or a reconstructed history of earlier changes.
