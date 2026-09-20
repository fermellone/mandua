"""CLI for Mandu'a Jev adapter harness."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from adapters.jev.client import TypeSafeJevClient
from adapters.jev.harness import ManduaJevHarness


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for testing the Jev adapter harness."""
    parser = argparse.ArgumentParser(
        description=(
            "Route and execute Mandu'a memory operations using TypeSafe AI's Jev System One model."
        ),
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path.cwd(),
        help="Path to the target Git repository (default: current working directory).",
    )
    parser.add_argument(
        "query",
        type=str,
        help="Natural language intent, task description, or query for memory inspection.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply mutations if Jev routes to a write operation.",
    )
    parser.add_argument(
        "--path",
        type=str,
        default=None,
        help="Optional path inside the repository for targeted operations.",
    )
    parser.add_argument(
        "--line",
        type=int,
        default=None,
        help="Optional line number for the 'why' operation.",
    )
    parser.add_argument(
        "--format",
        choices=["human", "json"],
        default="human",
        help="Output format (default: human).",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Force offline mock evaluation even if an API key is present.",
    )

    args = parser.parse_args(argv)

    client = TypeSafeJevClient(mock_mode=args.mock)
    harness = ManduaJevHarness(args.repo, client=client)

    result = harness.evaluate_and_run(
        args.query,
        apply=args.apply,
        path=args.path,
        line=args.line,
    )

    if args.format == "json":
        print(json.dumps(result.to_dict(), indent=2))
        return 0

    route = result.route
    print(f"Jev Decision: operation='{route.operation}' (confidence: {route.confidence:.2f})")
    print(f"Risk Score: {route.risk_score:.1f}/10 | Execution Tier: {route.execution_tier}")
    print(f"Policy Summary: {route.reasoning_summary}")
    print("-" * 60)

    if result.executed and result.memory_result is not None:
        mem = result.memory_result
        print(f"Mandu'a Result ({mem.operation}):")
        print(f"  Answer: {mem.answer}")
        print(f"  Confidence: {mem.confidence.value}")
        print(f"  Applied: {mem.applied}")
        if mem.warnings:
            print("  Warnings:")
            for w in mem.warnings:
                print(f"    - {w}")
    elif result.requires_confirmation:
        print(
            "Action required: Operation was held back by safety policy. "
            "Pass --apply to confirm execution."
        )
    else:
        print(f"Result: {result.explanation}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
