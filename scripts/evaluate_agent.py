"""Run the versioned durable HTTP/SSE evaluation suite; default is fully offline."""

import argparse
import asyncio
import json
from pathlib import Path

from travel_agent.evaluation import load_suite, run_evaluation
from travel_tools.config import Settings


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=Path("evals/agent_cases.json"))
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--live", action="store_true", help="Use the configured real model/key.")
    parser.add_argument(
        "--live-providers",
        action="store_true",
        help="With --live, use real providers instead of recorded evidence.",
    )
    parser.add_argument(
        "--output", type=Path, default=Path("private/evaluations/agent-report.json")
    )
    args = parser.parse_args()
    if args.live_providers and not args.live:
        parser.error("--live-providers requires --live; both can consume configured quotas.")
    mode = "live_providers" if args.live_providers else "live_model" if args.live else "offline"
    report = await run_evaluation(
        load_suite(args.suite),
        mode=mode,
        settings=Settings() if args.live else Settings(_env_file=None),
        case_ids=args.case_ids,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "mode": mode,
                "output": str(args.output.resolve()),
                "cases": [
                    {
                        "id": case["case_id"],
                        "outcome": case["run_outcome"],
                        "metrics": case["metrics"],
                    }
                    for case in report["cases"]
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if any(
        case["run_outcome"] == "harness_error"
        or any(check["status"] == "failed" for check in case.get("assertions", []))
        for case in report["cases"]
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
