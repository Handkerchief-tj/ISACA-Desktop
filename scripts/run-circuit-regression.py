"""Run deterministic SLiCAP 5.2.1 regression over a directory of `.cir` files."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from isaca_api.models import AnalysisRequest
from isaca_api.parameters import numeric_substitutions
from isaca_api.slicap_adapter import SLiCAP521Adapter, installed_slicap_version


def _read_netlist(path: Path) -> tuple[str, str]:
    """Read common Chinese/Windows encodings without modifying the source file."""

    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return path.read_text(encoding=encoding), encoding
        except UnicodeDecodeError:
            continue
    raise ValueError(f"Cannot decode netlist: {path}")


def _git_revision(root: Path) -> str | None:
    """Return the current revision when the runner is inside a Git checkout."""

    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _checkpoint(path: Path, payload: dict) -> None:
    """Persist after every case so an interrupted long run remains inspectable."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def run_regression(library_root: Path, output: Path, numeric: bool) -> dict:
    """Parse every netlist and optionally analyze numerically complete cases."""

    repository = Path(__file__).resolve().parents[1]
    files = sorted(library_root.rglob("*.cir"), key=lambda item: str(item).lower())
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "git_revision": _git_revision(repository),
        "slicap_version": installed_slicap_version(),
        "library_root": str(library_root.resolve()),
        "numeric_requested": numeric,
        "cases": [],
        "summary": {},
    }
    adapter = SLiCAP521Adapter(output.parent / "regression-jobs")
    for index, path in enumerate(files, start=1):
        started = time.perf_counter()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        record = {
            "index": index,
            "path": str(path.relative_to(library_root)),
            "sha256": digest,
        }
        try:
            text, encoding = _read_netlist(path)
            record["encoding"] = encoding
            probe = adapter.prepare_document(
                AnalysisRequest(netlist_text=text, modes=[], numeric=False)
            )
            _, missing = numeric_substitutions(probe.parameters)
            modes = ["laplace", "pz"] if numeric and not missing else []
            request = AnalysisRequest(netlist_text=text, modes=modes, numeric=bool(modes))
            result = adapter.analyze(f"case-{index:03d}-{digest[:10]}", request)
            record.update(
                {
                    "status": "numeric_passed" if modes else "structure_passed",
                    "numeric_missing": missing,
                    "diagnostics": [item.model_dump(mode="json") for item in probe.diagnostics],
                    "parameter_sources": dict(
                        Counter(item.source.value for item in probe.parameters)
                    ),
                    "flattened_elements": len(result["flattened_circuit"]["elements"]),
                    "flattened_nodes": len(result["flattened_circuit"]["nodes"]),
                    "analyses": result["analyses"],
                }
            )
        except Exception as error:
            record.update(
                {"status": "failed", "error": f"{type(error).__name__}: {error}"}
            )
        record["duration_seconds"] = round(time.perf_counter() - started, 6)
        payload["cases"].append(record)
        payload["summary"] = dict(Counter(case["status"] for case in payload["cases"]))
        payload["summary"]["completed"] = len(payload["cases"])
        payload["summary"]["total"] = len(files)
        _checkpoint(output, payload)
        print(f"[{index:02d}/{len(files):02d}] {record['status']}: {record['path']}")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--library-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--numeric", action="store_true")
    args = parser.parse_args()
    result = run_regression(args.library_root, args.output, args.numeric)
    print(json.dumps(result["summary"], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
