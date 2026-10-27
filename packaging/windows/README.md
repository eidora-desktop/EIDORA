# Windows releases: EXE and Microsoft Store MSIX

The Unsigned development bundles workflow builds one x64 PyInstaller application folder.
Inno Setup creates the direct-download EXE; MakePRI and MakeAppx create an unsigned
MSIX for manual Partner Center submission. MSIX is a full-trust desktop application,
not an AppContainer conversion. It runs without elevation.

## One-time Store configuration

Copy these exact **Product identity** values from the reserved Partner Center app
into `store.json` (they are public identifiers, not secrets):

| Configuration field | Partner Center value |
| --- | --- |
| `identity_name` | Package/Identity/Name |
| `publisher` | Package/Identity/Publisher, including `CN=` |
| `publisher_display_name` | Package/Properties/PublisherDisplayName |
| `display_name` | Reserved app name used by this listing |

The identity fields must match the account owner’s Partner Center reservation.
Do not invent them. MSIX, combined Windows, and `all` development builds fail early
when these values are missing; EXE-only development builds remain usable.

EIDORA's release version comes from `src/eidora_version.py`. App UI, reports,
installer filenames, and Windows executable FileVersion/ProductVersion use that
version. Only the MSIX manifest identity uses `(major + 1).minor.patch.0`:
`0.2.0` maps to `1.2.0.0`, `0.2.1` to `1.2.1.0`, and `1.0.0` to `2.0.0.0`.
The major offset remains in place after 1.0 so Store updates stay ordered.
Windows Settings and Store-managed package information may show this package
version. Packaging records both versions in `msix-build-report.json`.

Do not set `package_version` in `store.json`; packaging derives it automatically.
Set `last_submitted_version` to the highest package version uploaded to this
reservation, or `null` before the first submission. Update this ledger after
upload, even if certification fails. A subsequent submission needs a newer
EIDORA release whose derived package version exceeds that ledger. The fourth
component remains zero. Local validation cannot query Partner Center; confirm
its submission history before upload. Test rebuilds may reuse a version until
it is submitted.

```powershell
python automation/package_msix.py --validate-config-only
```

## Build and download

Actions → Unsigned development bundles → Run workflow → target:

| Target | Windows output |
| --- | --- |
| `windows` | EXE only |
| `windows-msix` | MSIX only, without Inno Setup or signing credentials |
| `windows-both` | EXE and MSIX from one application build |
| `all` | All platforms; Windows Store MSIX only |

Official Windows candidates are built unsigned on the public runner and verified
under private release control; source tags do not trigger builds.
Public EXE builds are unsigned development artifacts only.
The optional local EXE signing tools remain available, but direct-download EXE
distribution would require a separate certificate and release review.
The MSIX **container** is always unsigned for submission, including when its payload
contains the Authenticode-signed executable from a combined build. Microsoft signs
the package during publication. No Store API credentials are needed.

Download `eidora-candidate-windows`. It contains the `.msix`, `SHA256SUMS`,
`candidate.json`, the packaged self-test report, and dependency versions.
Download `eidora-diagnostics-eidora-windows-x86_64` as well for
`build-output/store/msix-build-report.json`. Both uploads expire after one day.
Verify the candidate's source commit, workflow run, and hashes, and preserve the
original submission bytes and reports locally. Do not attach the unsigned MSIX
to a public GitHub release or present it as a normal double-click installer.

For local packaging on Windows, install the project's release dependencies and
the Windows SDK, then use the already-built application folder:

```powershell
python automation/package_msix.py --bundle-dir build-output/dist --output-dir build-output/store --version 0.2.0
```

Use `--sdk-bin` to specify a particular SDK's **x64** tools directory. Otherwise
the newest installed SDK containing both tools is selected. The build report
records the tool paths and commit. Packaging stages a copy, renders Store images
from the approved local vectors, builds a PRI, enables MakeAppx validation, and
compares every staged payload file against the archive before publication. Existing
EXE/DMG/Linux icon exports are unchanged.

## Project storage

Installed Store builds detect their identity through `GetCurrentPackageFullName`.
New installs use the platform Documents folder's `EIDORA\Projects` directory.
Existing `%USERPROFILE%\EIDORA\workspace_projects` workspaces remain in use until
explicit relocation. Both locations, including recent-project records and the
starter receipt, survive package removal.
Models and disposable caches remain in application-data storage and may be removed
on uninstall. New EXE installs use the same Documents default. An explicit
`EIDORA_WORKSPACE_ROOT` still takes precedence; choose a durable location outside
AppData and the installation folder for a Store override.

Existing projects are not moved or copied automatically. Use the existing project
opening/import workflows. EXE and Store editions can coexist; close one before
opening the same project in the other. External media links must remain accessible
on the test computer. The project workspace is not a backup.
See [storage and cleanup](../../docs/storage.md) for explicit relocation and removal.

## Validate on Windows before submission

The declared minimum is Windows 10 22H2 (`10.0.19045.0`); the initial Windows 11
validation target is 24H2 (`10.0.26100.0`). These are **targets to verify**, not
evidence of passing tests. Native Windows ARM and S mode are not claimed.
Hosted Windows Server CI validates packaging but does not replace desktop QA.

Use disposable Windows test machines/VM snapshots. The SDK and PowerShell are
test-harness requirements; end users should need neither Python nor developer tools.
Use an interactive desktop session for installed tests and WACK.

1. Preserve the original unsigned submission package. Generate a signed copy:

   ```powershell
   ./automation/prepare_msix_test.ps1 -Package C:\QA\EIDORA-0.2.0-windows-x64-store.msix -OutputDirectory C:\QA\test-copy
   ```

   This generates a 30-day certificate matching the publisher, signs a separate
   test copy, exports only its public `.cer`, and deletes the temporary private key.

2. In an elevated PowerShell on the disposable test machine, trust that public
   certificate. Then close the elevated shell:

   ```powershell
   Import-Certificate -FilePath C:\QA\test-copy\EIDORA-test-only.cer -CertStoreLocation Cert:\LocalMachine\TrustedPeople
   ```

3. In a **non-elevated** interactive PowerShell, validate the installed copy:

   ```powershell
   ./automation/validate_msix.ps1 -SubmissionPackage C:\QA\EIDORA-0.2.0-windows-x64-store.msix -TestPackage C:\QA\test-copy\EIDORA-test-only.msix -ReportDirectory C:\QA\reports\win10-22h2
   ```

   Use a new report directory each time. The script checks trust, signature, matching
   manifest/block map, installation, activation by AUMID, self-test success, actual
   package identity, and default workspace location. It keeps the package installed
   for visible QA. Logs record the unsigned submission hash and OS version. The
   smoke test uses temporary projects; it does not modify the real workspace.

4. Repeat on Windows 11 24H2. On both systems, manually verify:
   - Standard-user Start menu launch and correct icons at normal and high DPI.
   - Offline starter installation; create/save/reopen a project and import an EXE
     project, including access to linked external files.
   - Discovery pan/zoom/pick/filter/pins, video thumbnails, model download, CPU
     embeddings, background jobs, and advertised exports.
   - Run from an unrelated working directory and ensure no files are written into
     the package installation directory. Exercise normal application-data caches.
   - With the previous Store version installed, create and save a project, install
     the candidate without removing the old version, and reopen that project.
     For the first Store submission, if no previous package was uploaded, record
     upgrade testing as not applicable and test fresh installation/removal.
     Later releases must test the actual previous-to-candidate upgrade. Do not
     override the derived Store version to manufacture a synthetic release.
   - Remove the candidate with `Remove-AppxPackage`, verify workspace files and
     `.starter-collection.json` remain, reinstall, and reopen the saved project.
     Deleting the starter project must not cause it to be seeded again.

5. Run the Windows App Certification Kit against the installed package in an
   interactive session, using its reported full name:

   ```powershell
   appcert.exe reset
   appcert.exe test -packagefullname <package-full-name> -reportoutputpath C:\QA\reports\wack.xml
   ```

   Resolve failures before submission. Keep the WACK report and a dated manual QA
   record beside each installed-validation report. The script intentionally records
   manual GPU, upgrade/uninstall, and WACK tests as `not_run`; it does not certify them.
   Include real Intel/AMD Windows hardware for GPU and large-collection responsiveness
   testing; a VM alone is insufficient.

Restore the VM snapshot or remove the test package and test certificate from
TrustedPeople when finished. Never attach a private certificate or the test-signed
package to the public release. Testing signs a copy; it does not change the Store
submission hash.

## Manual submission and subsequent updates

Upload the original unsigned `.msix` to the reserved app in Partner Center. Confirm
identity/version acceptance, language, availability, age rating, description,
screenshots, support URL, and privacy-policy URL. In certification notes explain
that EIDORA is a full-trust PySide desktop application using native rendering and
child processes for analysis/media work; identify the offline starter and steps to
exercise project creation and export. Describe optional model downloads accurately.

Do not submit until both OS targets, visible hardware tests, upgrade/uninstall,
and WACK pass. Initial approval/publication remains a separate Microsoft decision.
After approval, install from the Store and confirm startup and the next Store-managed
update with saved projects. Later updates use the same identity and increasing
package versions; no in-app updater is added for this channel.

References: [desktop manifest and tools](https://learn.microsoft.com/en-us/windows/msix/desktop/desktop-to-uwp-manual-conversion),
[accepted packages](https://learn.microsoft.com/en-us/windows/apps/publish/publish-your-app/msix/upload-app-packages),
[Store version requirements](https://learn.microsoft.com/en-us/windows/apps/publish/publish-your-app/msix/app-package-requirements),
[certification](https://learn.microsoft.com/en-us/windows/apps/publish/publish-your-app/msix/app-certification-process).
