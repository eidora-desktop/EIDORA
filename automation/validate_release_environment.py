"""Validate tag/version alignment and required platform signing credentials."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


REQUIRED_SIGNING_ENVIRONMENT = {
    "macOS": (
        "MACOS_CERTIFICATE_P12_BASE64",
        "MACOS_CERTIFICATE_PASSWORD",
        "MACOS_SIGNING_IDENTITY",
        "APPLE_ID",
        "APPLE_TEAM_ID",
        "APPLE_APP_PASSWORD",
    ),
    "Windows": (
        "WINDOWS_CERTIFICATE_PFX_BASE64",
        "WINDOWS_CERTIFICATE_PASSWORD",
    ),
    "Linux": (),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=tuple(REQUIRED_SIGNING_ENVIRONMENT), required=True)
    parser.add_argument("--ref", default=os.environ.get("GITHUB_REF", ""))
    parser.add_argument("--windows-format", choices=("exe", "msix", "both"), default="exe")
    arguments = parser.parse_args()
    if not arguments.ref.startswith("refs/tags/"):
        print("Manual bundle build: signing credentials are optional.")
        return 0

    root = Path(__file__).resolve().parents[1]
    namespace: dict[str, object] = {}
    exec((root / "src" / "eidora_version.py").read_text(encoding="utf-8"), namespace)
    expected_tag = f"v{namespace['__version__']}"
    actual_tag = arguments.ref.removeprefix("refs/tags/")
    errors = []
    if actual_tag != expected_tag:
        errors.append(f"tag {actual_tag!r} does not match application version {expected_tag!r}")
    required = REQUIRED_SIGNING_ENVIRONMENT[arguments.platform]
    if arguments.platform == "Windows" and arguments.windows_format == "msix":
        required = ()  # Microsoft signs the Store package after certification.
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        errors.append(f"missing {arguments.platform} release secrets: {', '.join(missing)}")
    if errors:
        print("Release validation failed: " + "; ".join(errors), file=sys.stderr)
        return 1
    print(f"Release environment accepted for {actual_tag} on {arguments.platform}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
