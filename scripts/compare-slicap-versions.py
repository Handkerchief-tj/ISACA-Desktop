"""Compare SLiCAP 4.0.8 and 5.2.1 on numeric cases selected by regression."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import sympy as sp


def _run_worker(python: Path, netlist: Path, project: Path, output: Path) -> str | None:
    worker = Path(__file__).with_name("slicap-version-worker.py")
    completed = subprocess.run(
        [
            str(python),
            str(worker),
            "--netlist",
            str(netlist),
            "--project",
            str(project),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if completed.returncode == 0:
        return None
    return (completed.stderr or completed.stdout).strip()


def _root_error(left: list[dict], right: list[dict]) -> float | None:
    if len(left) != len(right):
        return None
    remaining = [complex(item["real"], item["imag"]) for item in right]
    worst = 0.0
    for item in left:
        value = complex(item["real"], item["imag"])
        closest = min(range(len(remaining)), key=lambda index: abs(remaining[index] - value))
        match = remaining.pop(closest)
        worst = max(worst, abs(match - value) / max(abs(value), abs(match), 1.0))
    return worst


def _transfer_comparison(left: str, right: str) -> tuple[bool, float]:
    s = sp.Symbol("s")
    left_expr = sp.sympify(left)
    right_expr = sp.sympify(right)
    symbolic_equal = sp.simplify(sp.together(left_expr - right_expr)) == 0
    worst = 0.0
    for frequency in (1.0, 1e2, 1e4, 1e6, 1e8, 1e10):
        left_value = complex(sp.N(left_expr.subs(s, sp.I * frequency)))
        right_value = complex(sp.N(right_expr.subs(s, sp.I * frequency)))
        worst = max(
            worst,
            abs(left_value - right_value) / max(abs(left_value), abs(right_value), 1e-30),
        )
    return symbolic_equal, worst


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--legacy-python", type=Path, required=True)
    parser.add_argument("--next-python", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    manifest_path = args.manifest.resolve()
    output_path = args.output.resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    library_root = Path(manifest["library_root"])
    selected = [case for case in manifest["cases"] if case["status"] == "numeric_passed"]
    result = {"cases": [], "summary": {"total": len(selected)}}
    jobs = output_path.parent / "version-comparison-jobs"
    for index, case in enumerate(selected, start=1):
        netlist = library_root / case["path"]
        case_root = jobs / case["sha256"][:12]
        legacy_output = case_root / "legacy.json"
        next_output = case_root / "next.json"
        legacy_error = _run_worker(
            args.legacy_python, netlist, case_root / "legacy-project", legacy_output
        )
        next_error = _run_worker(
            args.next_python, netlist, case_root / "next-project", next_output
        )
        record = {"path": case["path"], "sha256": case["sha256"]}
        if legacy_error or next_error:
            record.update(
                {"status": "failed", "legacy_error": legacy_error, "next_error": next_error}
            )
        else:
            legacy = json.loads(legacy_output.read_text(encoding="utf-8"))
            next_result = json.loads(next_output.read_text(encoding="utf-8"))
            symbolic_equal, transfer_error = _transfer_comparison(
                legacy["laplace"], next_result["laplace"]
            )
            pole_error = _root_error(legacy["poles"], next_result["poles"])
            zero_error = _root_error(legacy["zeros"], next_result["zeros"])
            passed = (
                transfer_error <= 1e-6
                and pole_error is not None
                and pole_error <= 1e-6
                and zero_error is not None
                and zero_error <= 1e-6
            )
            record.update(
                {
                    "status": "passed" if passed else "different",
                    "legacy_version": legacy["slicap"],
                    "next_version": next_result["slicap"],
                    "transfer_symbolically_equal": symbolic_equal,
                    "transfer_sample_max_relative_error": transfer_error,
                    "pole_max_relative_error": pole_error,
                    "zero_max_relative_error": zero_error,
                }
            )
        result["cases"].append(record)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[{index:02d}/{len(selected):02d}] {record['status']}: {case['path']}")
    result["summary"].update(
        {
            status: sum(case["status"] == status for case in result["cases"])
            for status in ("passed", "different", "failed")
        }
    )
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result["summary"], indent=2))


if __name__ == "__main__":
    main()
