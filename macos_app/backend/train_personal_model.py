"""Train a participant's app model; report one JSON result on stdout."""

import argparse
import json
import logging
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--participant", type=int, required=True)
    parser.add_argument("--data-root", type=Path, default=REPO_ROOT / "data")
    parser.add_argument("--output-dir", type=Path, default=REPO_ROOT / "ml_pipeline/models/participants")
    args = parser.parse_args()
    protocol = sys.stdout
    sys.stdout = sys.stderr
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        from pipeline.personalize_app import personalize_app

        path, summary = personalize_app(
            participant=args.participant, data_root=args.data_root, output_dir=args.output_dir,
        )
        result = {"event": "trained", "path": str(path), **summary}
        status = 0
    except Exception as exc:
        logging.exception("Personal model training failed")
        result = {"event": "error", "message": str(exc)}
        status = 2
    protocol.write(json.dumps(result, allow_nan=False) + "\n")
    protocol.flush()
    return status


if __name__ == "__main__":
    sys.exit(main())
