"""Python 3.10-compatible entry point executed only by an external vision pack."""

import argparse
import json
import os
from pathlib import Path
import traceback


def main():
    """Probe both frameworks before inference and publish a machine-readable result."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--device", choices=("cpu", "cuda"), required=True)
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    result = {"status": "failed", "device": args.device}
    try:
        if args.device == "cpu":
            os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
        import torch
        import paddle
        capabilities = {
            "torch_cuda": torch.cuda.is_available(),
            "paddle_cuda": paddle.is_compiled_with_cuda() and paddle.device.cuda.device_count() > 0,
            "torch_version": torch.__version__,
            "paddle_version": paddle.__version__,
        }
        result["capabilities"] = capabilities
        if args.device == "cuda" and not all(capabilities[key] for key in ("torch_cuda", "paddle_cuda")):
            result["code"] = "gpu_unavailable"
            raise RuntimeError("Both Torch and Paddle must support CUDA in the selected NVIDIA pack.")
        from netlens import CircuitAnalysisPipeline
        from netlens.utils.config_loader import load_config
        artifacts = CircuitAnalysisPipeline(load_config(Path(args.config))).run(args.image, str(output))
        result.update(status="completed", artifacts={key: str(value) for key, value in artifacts.items()})
    except Exception as error:
        result["error"] = str(error)
        traceback.print_exc()
    (output / "vision-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
