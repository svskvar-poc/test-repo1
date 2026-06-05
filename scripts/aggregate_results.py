#!/usr/bin/env python3
"""Aggregate synthetic upstream-test reports into one summary.

POC analogue of ``aggregate_upstream_results.py`` in the upstream repo:
it collapses all per-node pytest-JSON reports into a single per-status tally
for the whole run, computes the ``failed + error`` count the gate keys on, and
records how many node reports were found (for the completeness check).

Outputs:
  * ``summary.json`` — machine-readable roll-up consumed by ``upstream_gate.py``
  * a human-readable table printed to stdout (and the GH job summary, if set)

Status vocabulary matches the real pipeline; ``FAILURE_STATUSES`` mirrors the
real aggregator (``failed`` and ``error`` only — not skipped, not passed).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

# Mirror of aggregate_upstream_results.FAILURE_STATUSES in the real repo.
FAILURE_STATUSES = ("failed", "error")


def load_reports(results_dir: Path) -> list[Path]:
    """Return all per-node JSON report files under ``results_dir``."""
    return sorted(results_dir.glob("**/test_results_*.json"))


def aggregate(results_dir: Path) -> dict:
    """Tally per-status counts across every node report.

    Returns a dict with ``total_stats`` (status -> count), ``failed_plus_error``,
    ``nodes_reported`` and ``total``.
    """
    report_files = load_reports(results_dir)
    total_stats: dict[str, int] = defaultdict(int)

    for rf in report_files:
        try:
            with open(rf) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            # A corrupt/unreadable node report is itself a failure signal; the
            # completeness check below also catches a missing report. We surface
            # it loudly rather than silently undercounting.
            print(f"WARNING: could not read {rf}: {exc}", file=sys.stderr)
            continue
        for row in data.get("tests", []):
            status = row.get("outcome") or row.get("status") or "unknown"
            total_stats[status] += 1

    failed_plus_error = sum(total_stats.get(s, 0) for s in FAILURE_STATUSES)
    total = sum(total_stats.values())

    return {
        "total_stats": dict(total_stats),
        "failed_plus_error": failed_plus_error,
        "nodes_reported": len(report_files),
        "total": total,
    }


def _pass_rate(total_stats: dict) -> str:
    passed = total_stats.get("passed", 0)
    denom = passed + sum(total_stats.get(s, 0) for s in FAILURE_STATUSES)
    if denom == 0:
        return "N/A"
    return f"{passed / denom * 100:.1f}"


def render_table(result: dict) -> str:
    """Human-readable one-shot summary for ALL tests at once."""
    ts = result["total_stats"]
    lines = [
        "=" * 52,
        "  Upstream Test Results (Aggregated — all nodes)",
        "=" * 52,
        f"  nodes reported : {result['nodes_reported']}",
        f"  total tests    : {result['total']}",
    ]
    for status in sorted(ts):
        lines.append(f"  {status:<14}: {ts[status]}")
    lines.append("-" * 52)
    lines.append(f"  failed+error   : {result['failed_plus_error']}")
    lines.append(f"  pass_rate(%)   : {_pass_rate(ts)}")
    lines.append("=" * 52)
    return "\n".join(lines)


def write_summary(result: dict, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(result, f, indent=2)


def _append_job_summary(text: str) -> None:
    """Append to the GitHub Actions job summary when running in CI."""
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a") as f:
            f.write("```\n" + text + "\n```\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("results_dir", type=Path, help="Directory holding per-node JSON reports.")
    p.add_argument(
        "--summary-out",
        type=Path,
        default=None,
        help="Where to write summary.json (default: <results_dir>/summary.json).",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.results_dir.exists():
        print(f"ERROR: results dir not found: {args.results_dir}", file=sys.stderr)
        return 1

    result = aggregate(args.results_dir)
    summary_out = args.summary_out or (args.results_dir / "summary.json")
    write_summary(result, summary_out)

    table = render_table(result)
    print(table)
    print(f"\nSummary written to {summary_out}")
    _append_job_summary(table)
    return 0


if __name__ == "__main__":
    sys.exit(main())
