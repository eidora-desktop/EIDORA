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
- Test UI changes in light and dark themes; include reproduction steps and describe
  the visual results in the PR. Keep screenshots local; do not upload or commit
  them. Describe platform-specific checks and any checks not run.
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
Ubuntu needs the native packages listed in `packaging/linux/apt-packages.txt`;
run `python automation/check_qt_runtime.py` after installing them.
The maintainer's application and automation test suites and agent instructions
are kept privately. The maintainer runs those suites against reviewed changes;
passing public CI alone does not certify application behavior.

`Source gate / Validate public checkout` is the required CI check. An external
contribution may wait for maintainer approval before CI runs. CI approval only
allows validation; it does not approve merging or signing. Fix failures rather
than weakening checks or requesting privileged CI access.

## Review and official distribution

The maintainer reviews contributions, including workflows, dependencies,
packaging, and CODEOWNERS. Only the maintainer has write/merge access. GitHub
requires a PR, passing Source Gate, an up-to-date branch, and resolved review
conversations; it does not require a second approval while there is one writer.
CODEOWNERS routes review requests to the maintainer.

After reviewing a change and its validation, the maintainer may select squash
auto-merge for that PR. GitHub merges only when its requirements pass, then
deletes the remote branch. Auto-merge is never enabled automatically for incoming
contributions or dependency updates. External contributors cannot enable it or
merge. Revisit the review policy before granting another person write access.

Maintainer development uses one local review before committing, pushing, and
queuing the approved change for merge. Changed source must be validated and
reviewed again; failed checks block merging. Retrying the same approved change
does not require another approval. Merging does not build or publish a release.

Public builds are unsigned development artifacts. Official release candidates
are created separately from a reviewed full commit SHA, using private release
control. Apple credentials, publishing credentials, and Partner Center access
are never part of this repository. See [release policy](packaging/RELEASING.md).

Report suspected vulnerabilities through the private process in
[SECURITY.md](SECURITY.md), not in public issues. Contributions use the existing
[MIT license](LICENSE).
