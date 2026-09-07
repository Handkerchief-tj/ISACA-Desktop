"""Analyze one numeric netlist in the active Python/SLiCAP environment."""

from __future__ import annotations

import argparse
import json
import locale
import os
from importlib.metadata import version
from pathlib import Path


def _read_text(path: Path) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"Cannot decode {path}")


def _working_text(text: str) -> str:
    lines = text.splitlines()
    if lines and any(ord(character) > 127 for character in lines[0]):
        lines[0] = '"ISACA comparison circuit"'
    return "\n".join(lines) + "\n"


def _complex_values(values) -> list[dict[str, float]]:
    result = []
    iterable = [] if values is None else values
    for value in iterable:
        number = complex(value)
        result.append({"real": number.real, "imag": number.imag})
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--netlist", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    project = args.project.resolve()
    (project / "cir").mkdir(parents=True, exist_ok=True)
    encoding = locale.getpreferredencoding(False) or "utf-8"
    working = _working_text(_read_text(args.netlist))
    (project / "cir" / "input.cir").write_bytes(working.encode(encoding, errors="replace"))
    os.chdir(project)

    import SLiCAP as sl

    try:
        sl.initProject("SLiCAP version comparison", report_dirs=True)
    except TypeError:
        sl.initProject("SLiCAP version comparison")
    circuit = sl.makeCircuit("input.cir", imgWidth=None, expansion=False)
    if circuit is None or int(getattr(circuit, "errors", 0) or 0):
        raise RuntimeError("SLiCAP could not parse the comparison netlist")
    pardefs = getattr(circuit, "parDefs", None) or None
    laplace = sl.doLaplace(circuit, pardefs=pardefs, numeric=True)
    pz = sl.doPZ(circuit, pardefs=pardefs, numeric=True)
    payload = {
        "slicap": version("SLiCAP"),
        "laplace": str(laplace.laplace),
        "poles": _complex_values(pz.poles),
        "zeros": _complex_values(pz.zeros),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
