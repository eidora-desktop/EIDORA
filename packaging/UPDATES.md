# Website downloads and update checks

EIDORA's public entry points are:

- `https://eidora.org/download/`
- `https://eidora.org/releases/`
- `https://eidora.org/updates.json`

The website repository is
`https://github.com/eidora-desktop/eidora-desktop.github.io`.
GitHub Pages serves the static website and catalog using its existing
`www.eidora.org` custom domain; the apex domain redirects there over HTTPS.
Mac/Linux files live in versioned GitHub Releases in the application repository.
Windows downloads lead to Microsoft Store, which manages installation and updates.

## Catalog contract

The website's `updates.json` is the publication owner. Schema version 1 contains:

- `schema_version`: integer `1`.
- `latest`: a version string or null for each of `macos-arm64`, `macos-x86_64`,
  `windows-x86_64`, and `linux-x86_64`.
- `releases`: a map keyed by numeric `MAJOR.MINOR.PATCH`. Each record contains
  `channel` (`prototype`, `preview`, or `stable`), `published_at` (actual ISO date),
  `notes` (plain text), and `platforms` (available artifacts by platform key).

Each platform includes verified `requirements`. Mac/Linux entries include a
`downloads` array with `format`, `name`, exact versioned GitHub `url`, byte `size`,
and lowercase `sha256`. Windows includes a verified `store_url` and no installer
download. The website validates this complete contract before deployment.
See the website repository's `RELEASES.md` and `scripts/catalog.py` for publication
instructions and artifact validation.

The initial catalog has null recommendations and an empty release map. Do not
advertise a prepared version, draft release, or starter dataset as an available
application. Each platform recommendation advances only after that platform is
publicly available. Windows can remain at an older version during certification.

## Application behavior

Opening About does not access the network. Selecting Check for Updates from the
Help menu or About dialog fetches the catalog asynchronously with timeouts.
An explicitly recommended prototype or preview is eligible for update checks.
Numeric comparison handles versions such as 0.9.0 and 0.10.0 correctly.

Newer, current, locally newer, unavailable, unsupported-platform, malformed-data,
and connection-error states have separate messages. View Update opens
`/download/#PLATFORM`; Release Notes opens `/releases/#vVERSION`. URLs are built
from the fixed website origin, never from arbitrary response links.
Closing the dialog cancels the request and disconnects its exact signal handlers.

There is no automatic installer or startup polling. Linux and macOS users follow
the website's manual installation guidance. Windows users follow the Store link.
If the website catalog has not been deployed yet, a 404 is reported as unavailable
update information, never as “up to date.”

## Release sequence

Complete the approved signing/publication process first. Prepare the website's
catalog from the actual public assets, review it, run the website tests/build,
and merge the approved website change to publish its pages and identical JSON
together. Keep historical entries. Verify each platform download and both domain
names after publication. Never commit signing credentials or installer files
into the website repository.
