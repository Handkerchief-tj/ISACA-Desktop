"""Collect installed dependency license files for the Windows release."""

from __future__ import annotations

import argparse
import importlib.metadata as metadata
import re
import shutil
from pathlib import Path


PACKAGES = (
    "SLiCAP",
    "PySide6",
    "PySide6_Essentials",
    "PySide6_Addons",
    "shiboken6",
    "numpy",
    "scipy",
    "sympy",
    "matplotlib",
    "pydantic",
    "pydantic_core",
    "PyYAML",
    "graphviz",
)


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    parser.add_argument("--notices", type=Path, required=True)
    parser.add_argument("--katex-license", type=Path, required=True)
    args = parser.parse_args()

    destination = args.destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy2(args.notices, destination / "THIRD_PARTY_NOTICES.md")
    shutil.copy2(args.katex_license, destination / "KaTeX-LICENSE.txt")

    summary = ["ISACA Desktop third-party package inventory", ""]
    for package in PACKAGES:
        try:
            dist = metadata.distribution(package)
        except metadata.PackageNotFoundError:
            summary.append(f"{package}: not installed in the release environment")
            continue
        name = dist.metadata.get("Name", package)
        version = dist.version
        license_text = (
            dist.metadata.get("License-Expression")
            or dist.metadata.get("License")
            or "See copied license files and upstream distribution metadata"
        )
        summary.append(f"{name} {version}: {license_text}")
        package_dir = destination / f"{_safe_name(name)}-{_safe_name(version)}"
        copied = 0
        for entry in dist.files or ():
            filename = Path(str(entry)).name.lower()
            if not filename.startswith(("license", "copying", "notice", "copyright")):
                continue
            source = Path(dist.locate_file(entry))
            if not source.is_file():
                continue
            package_dir.mkdir(parents=True, exist_ok=True)
            target = package_dir / _safe_name(str(entry).replace("\\", "_").replace("/", "_"))
            shutil.copy2(source, target)
            copied += 1
        if copied == 0:
            summary.append(f"  warning: no license file was exposed by {name}'s wheel metadata")

    (destination / "PACKAGE_LICENSE_SUMMARY.txt").write_text(
        "\n".join(summary) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
