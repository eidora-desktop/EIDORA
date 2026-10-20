# Official releases and public development

The public repository owns source, architecture, dependency pins, and
reproducible unsigned packaging. A separate **private EIDORA_releases repository**
owned by the maintainer controls official signing and publication. The private
repository is not a submodule and is not needed to contribute or run EIDORA.

## Contribution path

1. Fork, make a focused change, and provide reproducible validation steps.
2. Open a pull request against `main`; the maintainer approves external CI runs.
3. Pass `Validate public checkout`, obtain the required code-owner review, and
   resolve conversations. New changes invalidate previous approvals.
4. The maintainer validates reviewed changes with private regression suites and
   merges. Passing CI or merging never starts signing or publishing.

The `Unsigned development bundles` workflow is manual, read-only, and has no
signing credentials. Its downloads are development artifacts, not official
releases. It can still create unsigned EXE packages for developer testing.

## Official release path (maintainer only)

The current path builds unsigned candidates on free standard GitHub-hosted
runners in this public repository: macos-15 (Apple Silicon), macos-15-intel,
windows-2022, and ubuntu-24.04. Only the maintainer can dispatch this repository's
candidate workflow on main. It has no signing or publication authority.
The private repository owns integrity verification, signing, and publication.
Local builds remain available. Candidate artifacts expire after one day; download
and verify them promptly, then remove the temporary Actions artifacts.

1. Review `Unreleased` and choose the version according to
   [the version policy](README.md#versioning). Update the source version and a
   dated changelog entry in a PR. Do not reuse a published version.
2. Review the full source SHA, pass Source Gate and the private regression suite,
   and merge before final publication. Keep candidate provenance tied to the
   exact source tree and commit used to build it.
3. Dispatch `Unsigned development bundles` on the reviewed main commit with
   target `all`. This builds both Mac architectures, Windows Store MSIX, and Linux
   DEB/AppImage, runs packaged self-tests, and records source and run provenance
   plus artifact hashes. Alternatively use the private local builder.
   Download candidates privately and verify their run, source SHA, and checksums.
   A build receipt does not constitute signing approval.
4. In a separate signing step, the maintainer's Mac verifies the candidate,
   signs a copy of the application, notarizes and staples the DMG, and verifies
   it. Signing uses the Mac's Keychain and does not launch application code.
   Windows produces an unsigned **MSIX submission package only**; Microsoft signs
   it for Store distribution. Linux produces DEB and AppImage artifacts.
5. Download candidates and perform native platform QA. Follow the installed-MSIX,
   upgrade/uninstall, WACK, and GPU checks in [the Windows guide](windows/README.md).
   Keep the original submission bytes and reports. Test signed Mac downloads on
   another Mac, including first launch and offline ticket validation.
6. Verify final artifact hashes, source provenance, version, and the dated
   changelog. Create a draft release on that exact reviewed source commit with
   a new version tag; mark prototype releases as pre-releases. Include only
   platforms that were actually built and validated.
7. Review and publish that draft manually. Upload the original unsigned MSIX
   manually in Partner Center. Store credentials never enter GitHub Actions.
   Record the submitted Store version even if certification later fails.
8. Prepare and review the website release catalog only for platforms that are
   publicly available. Publish the website's download/history pages and
   `updates.json` together. Advance Windows separately after Store availability
   is confirmed. See [website updates](UPDATES.md).

The draft includes validated signed/notarized DMGs, available Linux packages,
SHA256SUMS, and source provenance. MSIX and diagnostics are excluded. Checksums describe integrity;
SHA256SUMS is not a separate GPG signature. macOS authenticity comes from Developer
ID/notarization; Store package trust comes from Microsoft's signature.

## Security boundary

- Contributors have fork/PR access, not release-repository access.
- Public workflows never receive Apple credentials or cross-repository write tokens.
- Protect `main`, CODEOWNERS, and official `v*` tags with active GitHub rules.
- Keep public Actions tokens read-only, disable Actions PR approval, and require
  approval for all external fork contributors. Do not use self-hosted public runners.
- Keep all Actions pinned to full commit SHAs and review dependency changes.
- Do not run PR artifacts or public application scripts in signing/publishing jobs.
- Local signing requires explicit maintainer action. Optional hosted workflows
  also restrict the actor, triggering actor, branch, and repository visibility;
  their release credentials belong to dedicated environments.
- Environment reviewer availability depends on the GitHub plan. With one owner,
  self-review must be allowed for owner-initiated runs. An owner-only private
  repository and explicit dispatch remain the minimum boundary.

Before switching repository visibility, audit all Git history, release assets,
Actions logs/artifacts, collaborator permissions, and secret access. Rotate any
exposed credential before history cleanup. `.gitignore` does not remove tracked
or historical files. Enable private vulnerability reporting, secret scanning,
push protection, dependency alerts, and Actions code scanning where available.

Repository files express policy; **GitHub settings enforce it**. Follow the private
repository setup guide and verify its settings report before opening contributions.
