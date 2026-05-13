"""Command line entry points for standardized data builds."""

from __future__ import annotations

import argparse

from ..data.builder import DEFAULT_DATASET_DIRS, build_unified_processed_data
from ..data.core_dataset import build_metal_tech_core_dataset


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="Build the unified processed_data directory.")
    build.add_argument("--output-dir", default="processed_data")
    build.add_argument("--dataset-dirs", nargs="+", default=DEFAULT_DATASET_DIRS)
    core = subparsers.add_parser("build-metal-tech-core", help="Build the metal + tech core dataset and backtest report.")
    core.add_argument("--processed-dir", default="processed_data")
    core.add_argument("--output-dir", default="processed_data")
    core.add_argument("--reports-dir", default="outputs")

    args = parser.parse_args()
    if args.command == "build":
        summary = build_unified_processed_data(
            dataset_dirs=args.dataset_dirs,
            output_dir=args.output_dir,
        )
        print("Unified data build summary")
        for key, value in summary.items():
            print(f"{key}: {value}")
    if args.command == "build-metal-tech-core":
        summary = build_metal_tech_core_dataset(
            processed_dir=args.processed_dir,
            output_dir=args.output_dir,
            reports_dir=args.reports_dir,
        )
        print("Metal + tech core build summary")
        for key, value in summary.items():
            print(f"{key}: {value}")


if __name__ == "__main__":
    main()
