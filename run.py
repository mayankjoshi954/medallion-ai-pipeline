"""CLI entry point: python run.py [--stage all|bronze|silver|gold] [--file PATH] [--with-agents]"""
import argparse
import sys
from pathlib import Path

from src.log import configure_logging
from src.pipeline import DEFAULT_SOURCE, STAGES, run


def main() -> int:
    parser = argparse.ArgumentParser(description="Bronze -> silver -> gold ticket pipeline")
    parser.add_argument("--stage", choices=("all", *STAGES), default="all")
    parser.add_argument("--file", type=Path, default=DEFAULT_SOURCE, help="CSV to ingest into bronze")
    parser.add_argument("--with-agents", action="store_true",
                        help="run both LLM agents before silver and apply their output (needs an API key; cached)")
    args = parser.parse_args()
    configure_logging()
    return run(STAGES if args.stage == "all" else (args.stage,), args.file.resolve(), args.with_agents)


if __name__ == "__main__":
    sys.exit(main())
