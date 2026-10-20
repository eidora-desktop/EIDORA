# Contributing to EIDORA

Contributions use a fork and a focused pull request against `main`. Opening a PR
does not grant write access, signing access, or permission to publish an official
release. The maintainer reviews and merges contributions. Discuss substantial
workflow or architecture changes in an issue before implementing them.

## Development contract

Read [README.md](README.md) and the [architecture guide](docs/architecture.md).
These rules apply to human and assisted contributions.

- Preserve user workflows and keep changes small, explicit, and testable.
- Put UI wiring in controllers, durable layouts in Designer `.ui` files,
  application decisions in services, and persistence in repositories.
- Keep heavy computation outside UI code; preserve the native Discovery renderer
  contract and validate large-dataset responsiveness for rendering changes.
- Include reproducible validation steps for changed behavior. Use synthetic
  fixtures and temporary projects, never personal collections or credentials.
- Test UI changes in light and dark themes; include screenshots and reproduction
  steps in the PR. Describe platform-specific checks and any checks not run.
- Add release-relevant notes under `Unreleased` in `CHANGELOG.md`. Do not bump the
  application version or create official tags as part of an ordinary change.
- Do not include unrelated formatting, generated build output, local reports,
  signing material, API keys, or private user data.

## Required local checks

Install `python -m pip install -e '.[dev]'` in a virtual environment, then run:

```sh
PYTHONPATH=src python -m compileall -q src automation
python automation/source_gate.py
python automation/resource_inventory.py
```

PowerShell users can run the same Python commands without the `PYTHONPATH=src`
prefix after the editable install. Visual QA remains a separate requirement.
Ubuntu needs `libegl1`, `libgl1`, `libopengl0`, and `libxkbcommon0` for Qt imports.
The maintainer's application and automation test suites and agent instructions
are kept privately. The maintainer runs those suites against reviewed changes;
passing public CI alone does not certify application behavior.

`Source gate / Validate public checkout` is the required CI check. An external
contribution may wait for maintainer approval before CI runs. CI approval only
allows validation; it does not approve merging or signing. Fix failures rather
than weakening checks or requesting privileged CI access.

## Review and official distribution

Code-owner review is required, including changes to workflows, dependencies,
packaging, and CODEOWNERS itself. New commits dismiss earlier approvals. Resolve
review conversations before merge. The maintainer can merge their own reviewed
maintenance PR through the narrowly configured owner exception when there is no
second maintainer; external contributors receive no such exception.

Public builds are unsigned development artifacts. Official release candidates
are created separately from a reviewed full commit SHA, using private release
control. Apple credentials, publishing credentials, and Partner Center access
are never part of this repository. See [release policy](packaging/RELEASING.md).

Report suspected vulnerabilities through the private process in
[SECURITY.md](SECURITY.md), not in public issues. Contributions use the existing
[MIT license](LICENSE).
