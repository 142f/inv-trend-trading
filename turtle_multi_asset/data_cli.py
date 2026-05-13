"""Command line entry points for standardized data builds."""

from __future__ import annotations

import argparse

from .data_builder import DEFAULT_DATASET_DIRS, build_unified_processed_data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="Build the unified processed_data directory.")
    build.add_argument("--output-dir", default="processed_data")
    build.add_argument("--dataset-dirs", nargs="+", default=DEFAULT_DATASET_DIRS)

    args = parser.parse_args()
    if args.command == "build":
        summary = build_unified_processed_data(
            dataset_dirs=args.dataset_dirs,
            output_dir=args.output_dir,
        )
        print("Unified data build summary")
        for key, value in summary.items():
            print(f"{key}: {value}")


if __name__ == "__main__":
    main()
