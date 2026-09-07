"""Command-line entry point shared by source and packaged desktop builds."""

from __future__ import annotations

import argparse
from pathlib import Path

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="isaca-desktop")
    parser.add_argument("--project", help="Open one ISACA/SLiCAP project directory.")
    parser.add_argument("--file", help="Open one .slicap_sch or .cir file.")
    parser.add_argument(
        "--worker", nargs="+", metavar="ARG",
        help="Internal worker mode: --worker REQUEST or --worker ACTION REQUEST.",
    )
    return parser


def main() -> int:
    """Dispatch a GUI launch or one isolated worker request."""

    args = _parser().parse_args()
    if args.worker:
        from .worker import execute_worker

        request_path = args.worker[-1]
        return execute_worker(Path(request_path))
    from .app import run_desktop

    return run_desktop(project=args.project, file=args.file)


if __name__ == "__main__":
    raise SystemExit(main())
