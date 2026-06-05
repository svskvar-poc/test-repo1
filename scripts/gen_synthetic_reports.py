#!/usr/bin/env python3
"""Generate synthetic pytest-JSON reports for the merge-queue POC.

Stands in for the real trn2 upstream test run. Emits one JSON report per
simulated matrix node into an output directory. The total number of *failing*
tests across the run is randomized in the inclusive range [1, 8] (the POC's
chosen simulation range); with the default threshold of 5, runs land on both
sides of the gate.

Each report mirrors the subset of the pytest-json-report schema that the real
aggregator consumes: a top-level ``tests`` list of ``{nodeid, outcome}`` plus a
``created`` timestamp. Statuses use the same vocabulary as the real pipeline
(``passed`` / ``failed`` / ``error`` / ``skipped``).

Usage:
    python3 gen_synthetic_reports.py --output-dir <dir> [--nodes N]
                                     [--failures K] [--seed S]

If ``--failures`` is omitted, K is drawn uniformly from [1, 8].
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

# POC simulation range for the number of failing tests per run.
MIN_FAILURES = 1
MAX_FAILURES = 8

# How many "passing" tests to pad each run with, so a summary looks realistic
# and pass_rate is meaningful. Purely cosmetic for the POC.
PASSING_PER_RUN = 40


def _build_test_rows(total_failures: int, passing: int) -> list[dict]:
    """Build a flat list of pytest-style test rows.

    Failures are split roughly evenly between ``failed`` and ``error`` so the
    POC exercises both members of FAILURE_STATUSES. The remainder are
    ``passed``.
    """
    rows: list[dict] = []
    n_error = total_failures // 2
    n_failed = total_failures - n_error

    for i in range(n_failed):
        rows.append({"nodeid": f"sim/test_mod.py::TestSim::test_failed_{i}", "outcome": "failed"})
    for i in range(n_error):
        rows.append({"nodeid": f"sim/test_mod.py::TestSim::test_error_{i}", "outcome": "error"})
    for i in range(passing):
        rows.append({"nodeid": f"sim/test_mod.py::TestSim::test_passed_{i}", "outcome": "passed"})

    return rows


def _distribute(total: int, buckets: int) -> list[int]:
    """Split ``total`` items across ``buckets`` as evenly as possible."""
    if buckets <= 0:
        return []
    base, extra = divmod(total, buckets)
    return [base + (1 if b < extra else 0) for b in range(buckets)]


def generate(output_dir: Path, nodes: int, failures: int) -> dict:
    """Write ``nodes`` synthetic JSON reports; return a small manifest dict."""
    output_dir.mkdir(parents=True, exist_ok=True)

    failures_per_node = _distribute(failures, nodes)
    created = datetime.now(timezone.utc).isoformat()

    for node_idx in range(nodes):
        node_failures = failures_per_node[node_idx]
        rows = _build_test_rows(node_failures, PASSING_PER_RUN)
        report = {
            "created": created,
            "node": node_idx,
            "tests": rows,
        }
        out_path = output_dir / f"test_results_node{node_idx}.json"
        with open(out_path, "w") as f:
            json.dump(report, f, indent=2)

    manifest = {
        "nodes": nodes,
        "total_failures": failures,
        "failures_per_node": failures_per_node,
        "output_dir": str(output_dir),
    }
    return manifest


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output-dir", required=True, type=Path)
    p.add_argument("--nodes", type=int, default=3, help="Number of simulated matrix nodes.")
    p.add_argument(
        "--failures",
        type=int,
        default=None,
        help=f"Total failing tests; default random in [{MIN_FAILURES},{MAX_FAILURES}].",
    )
    p.add_argument("--seed", type=int, default=None, help="RNG seed for reproducibility.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.seed is not None:
        random.seed(args.seed)

    if args.failures is None:
        failures = random.randint(MIN_FAILURES, MAX_FAILURES)
    else:
        failures = args.failures

    if args.nodes <= 0:
        print("ERROR: --nodes must be >= 1", file=sys.stderr)
        return 2
    if failures < 0:
        print("ERROR: --failures must be >= 0", file=sys.stderr)
        return 2

    manifest = generate(args.output_dir, args.nodes, failures)
    print(json.dumps(manifest, indent=2))
    print(f"Generated {args.nodes} report(s) with {failures} total failure(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
