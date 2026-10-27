# Linux distribution

The DEB maintainer is `Tillmann Ohm <info@eidora.org>` and the homepage is
`https://eidora.org`. The DEB and AppImage ship AppStream developer metadata
linked to the existing `eidora.desktop` launcher. Preserve that launcher name
so upgrades do not create duplicate application entries.

Validate the metadata on Linux with:

```sh
appstreamcli validate --no-net packaging/linux/org.eidora.EIDORA.metainfo.xml
dpkg-deb --field EIDORA-0.2.0-linux-x86_64.deb Maintainer Homepage Version
dpkg-deb --contents EIDORA-0.2.0-linux-x86_64.deb
```

Check the installed app in the target distribution's software center as well.
Developer attribution is descriptive metadata, not identity verification.
Some software centers still show an unknown publisher for local DEBs.

## Planned APT repository

The proposed repository URL is `https://eidora.org/apt/`, with suite `stable`,
component `main`, and architecture `amd64`. Here `stable` names the update
channel; EIDORA itself remains prototype software. This repository is not live
until the maintainer publishes and verifies it. Do not advertise installation
commands or a signing-key fingerprint before that check.

Official repository generation and signing belong to the private release
repository. Public CI receives no signing key. APT verifies signed repository
metadata and the package hashes referenced by it; signing an individual DEB
does not automatically make a downloaded package trusted in App Center.

Installation instructions must use a dedicated keyring and `Signed-By` in a
Deb822 `.sources` file, with the full key fingerprint published separately for
verification. Never use `trusted=yes`, `allow-insecure`, or a globally trusted
`apt-key` import. Keep old versioned packages available for clients using cached
indexes, and never replace a published version with changed bytes.

Sources: [APT authentication](https://manpages.debian.org/testing/apt/apt-secure.8.en.html)
and [AppStream metadata](https://www.freedesktop.org/software/appstream/docs/chap-Metadata.html).
