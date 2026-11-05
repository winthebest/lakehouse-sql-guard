"""`sqlguard-bench`: reproducible benchmarks.

sqlguard-bench leakage prereg --seed 42     # thresholds fit on dev only
sqlguard-bench leakage run --seed 42 [--real] [--splits dev dev_v1b holdout_v2]
                           [--skip-v1] [--fixes] [--prereg PATH]
                           # PATH is required for a split nobody has seen
sqlguard-bench leakage duplicates --candidate holdout_v3 [--counts]   # or --candidate-dir DIR
sqlguard-bench leakage audit-labels --candidate holdout_v3   # or --candidate-dir DIR [--points N]
sqlguard-bench safety tier1
sqlguard-bench safety agent --model <id> --condition both --runs 3 --split dev [--replay DIR]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from bench.leakage.cases import DEFAULT_SPLITS, SPLITS


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="sqlguard-bench", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="suite", required=True)

    leakage = sub.add_parser("leakage", help="temporal leakage benchmark")
    leakage.add_argument("action", choices=["prereg", "run", "duplicates", "audit-labels"])
    leakage.add_argument("--seed", type=int, default=42)
    leakage.add_argument("--warehouse", type=Path, default=Path(".warehouse/bench"))
    leakage.add_argument("--real", action="store_true", help="also run on Online Retail II")
    leakage.add_argument(
        "--splits",
        nargs="+",
        default=list(DEFAULT_SPLITS),
        choices=SPLITS,
        help="template splits to score (run only)",
    )
    leakage.add_argument(
        "--prereg", type=Path, help="pre-registration to use (required for an unseen split)"
    )
    leakage.add_argument("--skip-v1", action="store_true", help="leave out the v1 detector row")
    leakage.add_argument(
        "--fixes", action="store_true", help="also report fix rates by verification level (run)"
    )

    safety = sub.add_parser("safety", help="agent-guardrail benchmark")
    safety.add_argument("action", choices=["tier1", "agent"])

    args, rest = parser.parse_known_args(argv)
    if args.suite == "leakage" and args.action == "duplicates":
        from bench.leakage import duplicates

        raise SystemExit(duplicates.main(rest))
    if args.suite == "leakage" and args.action == "audit-labels":
        from bench.leakage import audit

        shared = ["--seed", str(args.seed), "--warehouse", str(args.warehouse)]
        raise SystemExit(audit.main([*rest, *shared]))
    if args.suite == "leakage":
        from bench.leakage import run

        if args.action == "prereg":
            path = run.prereg(args.seed, args.warehouse, with_v1=not args.skip_v1)
            print(f"pre-registration written to {path}")
        else:
            path = run.run(
                args.seed,
                args.warehouse,
                args.real,
                splits=tuple(args.splits),
                with_v1=not args.skip_v1,
                with_fixes=args.fixes,
                prereg_path=args.prereg,
            )
            print(f"results written to {path}")
    elif args.action == "agent":
        from bench.safety import run as agent_run

        raise SystemExit(agent_run.main(rest))
    else:
        from bench.safety import tier1

        raise SystemExit(tier1.main(rest))


if __name__ == "__main__":
    main()
