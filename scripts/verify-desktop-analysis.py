"""Exercise the same isolated analysis worker as the desktop, without a GUI."""

from __future__ import annotations

import argparse
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main() -> int:
    """Run a reproducible RC or paper-demo job and retain every event/artifact."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=("rc_lowpass", "demo_2_numeric"), default="rc_lowpass")
    parser.add_argument("--symbolic", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    netlist = (repo / "examples" / "desktop" / (args.case + ".cir")).read_text(encoding="utf-8")
    request = {
        "action": "analysis", "job_id": args.case, "run_root": str(output),
        "result_path": str(output / "worker-result.json"),
        "payload": {"request": {
            "netlist_text": netlist,
            "modes": ["laplace", "pz", "matrix", "bode"] + (["symbolic"] if args.symbolic else []),
            "frequency_range_hz": [10, 1e11] if args.case == "demo_2_numeric" else [1, 1e5],
            "magnitude_error_db": 2, "phase_error_deg": 5, "max_steps_per_subrange": 10,
        }},
    }
    request_path = output / "request.json"
    request_path.write_text(json.dumps(request, indent=2), encoding="utf-8")
    environment = {**os.environ, "PYTHONPATH": str(repo / "src"), "PYTHONIOENCODING": "utf-8"}
    started = time.monotonic()
    with (output / "events.ndjson").open("w", encoding="utf-8") as stdout, (output / "worker.log").open("w", encoding="utf-8") as stderr:
        process = subprocess.run([sys.executable, "-m", "isaca_desktop", "--worker", str(request_path)],
                                 cwd=output, env=environment, stdout=stdout, stderr=stderr, timeout=args.timeout)
    envelope = json.loads((output / "worker-result.json").read_text(encoding="utf-8"))
    assert process.returncode == 0 and envelope["status"] == "completed", envelope
    result = envelope["result"]
    analyses = result["analyses"]
    if args.case == "rc_lowpass":
        assert abs(float(analyses["pz"]["poles"][0]) + 1000) < 1e-6
    else:
        assert len(analyses["pz"]["poles"]) == 3 and len(analyses["pz"]["zeros"]) == 2
    symbolic = analyses.get("symbolic", {})
    if args.symbolic:
        for interval in symbolic["frequency_results"]:
            assert interval["transfer"]["success"], interval["transfer"].get("error")
            assert interval["error"]["max_magnitude_error_db"] <= 2 + 1e-9
            assert interval["error"]["max_phase_error_deg"] <= 5 + 1e-9
        if args.case == "demo_2_numeric":
            import sympy as sp
            assert len(symbolic["frequency_results"]) == 4
            cmu, gx, cx, cpi, gm, gpi, Cl = sp.symbols("cmu gx cx cpi gm gpi Cl")
            equation_24 = -cmu * gx / (cx * (cmu + cpi))
            equation_22 = -(gm * cmu + (gx + gpi) * (cmu + cx + Cl)) / (
                (cmu + cpi) * (cmu + cx + Cl)
            )
            roots = [root for interval in symbolic["frequency_results"] for root in interval["target_roots"]]
            assert len(roots) == 5
            assert all(root.get("status") == "resolved" for root in roots), roots
            cluster_two_poles = [
                root
                for root in symbolic["frequency_results"][1]["target_roots"]
                if root.get("kind") == "pole"
            ]
            assert len(cluster_two_poles) == 1
            assert sp.simplify(sp.sympify(cluster_two_poles[0]["expression"]) - equation_22) == 0
            assert float(cluster_two_poles[0]["relative_root_error"]) > 0.3
            assert any(root.get("expression") and sp.simplify(sp.sympify(root["expression"]) - equation_24) == 0
                       for root in roots if root.get("kind") == "zero")
    summary = {
        "case": args.case, "seconds": time.monotonic() - started,
        "input_sha256": hashlib.sha256(netlist.encode()).hexdigest(),
        "versions": {name: version(name) for name in ("SLiCAP", "isaca-desktop", "PySide6", "sympy", "numpy")},
        "poles": analyses["pz"]["poles"], "zeros": analyses["pz"]["zeros"],
        "frequency_results": symbolic.get("frequency_results", []),
        "accepted_steps": symbolic.get("accepted_steps"), "diagnostics": result.get("diagnostics", []),
    }
    (output / "verification.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in summary.items() if key != "frequency_results"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
