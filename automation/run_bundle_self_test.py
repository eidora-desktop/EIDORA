"""Run the release self-test through a built application executable."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def _executable(bundle: Path) -> Path:
    candidates = (
        bundle / "Contents" / "MacOS" / "EIDORA",
        bundle / "EIDORA.exe",
        bundle / "EIDORA",
        bundle / "EIDORA.app" / "Contents" / "MacOS" / "EIDORA",
        bundle / "EIDORA" / "EIDORA.exe",
        bundle / "EIDORA" / "EIDORA",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError(f"No EIDORA application executable exists under {bundle}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        executable = _executable(arguments.bundle_dir.resolve())
        report_path = arguments.report.resolve()
        environment = dict(os.environ)
        environment.setdefault("QT_QPA_PLATFORM", "offscreen")
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.unlink(missing_ok=True)
        log_path = report_path.with_suffix(".process.log")
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                [str(executable), "--release-self-test", str(report_path)],
                stdout=log,
                stderr=subprocess.STDOUT,
                env=environment,
            )
            try:
                returncode = process.wait(timeout=300)
            except subprocess.TimeoutExpired:
                try:
                    if sys.platform == "win32":
                        subprocess.run(
                            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                            stdout=log, stderr=subprocess.STDOUT, timeout=15, check=False,
                        )
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=10)
                detail = report_path.read_text(encoding="utf-8") if report_path.exists() else "No checkpoint was written; startup did not reach the self-test."
                raise RuntimeError(f"Timed out after 300 seconds. Last report: {detail}")
        if returncode != 0:
            if report_path.is_file():
                failed_report = json.loads(report_path.read_text(encoding="utf-8"))
                raise RuntimeError(
                    f"release self-test exited with {returncode} "
                    f"(0x{returncode & 0xffffffff:08x}): "
                    f"{failed_report.get('error') or failed_report}"
                )
            raise RuntimeError(f"release self-test exited with {returncode}")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if report.get("ok") is not True:
            raise RuntimeError(str(report.get("error") or "self-test reported failure"))
        print(json.dumps(report, indent=2))
    except (OSError, RuntimeError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        print(f"Bundle self-test failed: {error}", file=sys.stderr)
        for suffix in (".process.log", ".trace.log"):
            log_path = arguments.report.resolve().with_suffix(suffix)
            if log_path.exists():
                print(f"{log_path.name}:\n{log_path.read_text(encoding='utf-8', errors='replace')[-20000:]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
