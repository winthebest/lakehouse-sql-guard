"""`sqlguard-bench`: reproducible benchmarks.

sqlguard-bench leakage prereg --seed 42     # thresholds on dev, written before held-out
sqlguard-bench leakage run --seed 42 [--real]
sqlguard-bench safety tier1
sqlguard-bench safety agent --model <id> --condition both --runs 3 --split dev [--replay DIR]
"""

from __future__ import annotations

import argparse
from pathlib import Path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="sqlguard-bench", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="suite", required=True)

    leakage = sub.add_parser("leakage", help="temporal leakage benchmark")
    leakage.add_argument("action", choices=["prereg", "run"])
    leakage.add_argument("--seed", type=int, default=42)
    leakage.add_argument("--warehouse", type=Path, default=Path(".warehouse/bench"))
    leakage.add_argument("--real", action="store_true", help="also run on Online Retail II")

    safety = sub.add_parser("safety", help="agent-guardrail benchmark")
    safety.add_argument("action", choices=["tier1", "agent"])

    args, rest = parser.parse_known_args(argv)
    if args.suite == "leakage":
        from bench.leakage import run

        if args.action == "prereg":
            print(f"pre-registration written to {run.prereg(args.seed, args.warehouse)}")
        else:
            print(f"results written to {run.run(args.seed, args.warehouse, args.real)}")
    elif args.action == "agent":
        from bench.safety import run as agent_run

        raise SystemExit(agent_run.main(rest))
    else:
        from bench.safety import tier1

        raise SystemExit(tier1.main(rest))


if __name__ == "__main__":
    main()
