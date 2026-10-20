# Changelog

Release-relevant changes are tracked here. Version selection follows the [versioning policy](packaging/README.md#versioning).

## Unreleased

## 0.2.0 — Prepared, not published

This minor prototype release adds Methods and Citations reporting and requires
BibTeX in ONNX model packages. Existing model packages without BibTeX must be
repackaged; existing saved runs remain readable. Set the actual publication date
when the reviewed release is published.

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

- Retain the Qt X11 platform library in Linux bundles so the packaged application
  can create its window.
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
