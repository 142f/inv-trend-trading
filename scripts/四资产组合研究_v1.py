"""Explicit research runner. Default formal mode fails closed with current inputs."""
import argparse
from inv_trend.application.四资产研究_v1 import run_research


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--study-id", required=True)
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--mode", choices=("FORMAL", "RESEARCH_ASSUMPTION"), default="FORMAL")
    args = parser.parse_args()
    run_research(args.study_id, data_root=args.data_root, mode=args.mode)


if __name__ == "__main__":
    main()
