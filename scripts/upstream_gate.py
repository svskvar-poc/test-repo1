#!/usr/bin/env python3
"""Merge-queue gate decision for the upstream-tests POC.

Single decision point the required status check keys on. Reads the aggregated
summary (from ``aggregate_results.py``), resolves the failure threshold, checks
that the run is complete, and exits 0 (PASS → queue may merge) or non-zero
(FAIL → PR dropped from the queue).

Threshold resolution order (first one set wins):
    1. --fail-threshold CLI arg
    2. UPSTREAM_FAILURE_THRESHOLD env var (fed from the Actions repo variable)
    3. failure_threshold in the config file
    4. hardcoded DEFAULT_THRESHOLD

Decision:
    incomplete run (nodes_reported < expected)  -> FAIL (don't trust the count)
    failed + error  > threshold                  -> FAIL (block merge)
    otherwise                                    -> PASS

Bypass is intentionally NOT handled here — overrides are native GitHub admin
break-glass via the ruleset bypass list (see DESIGN.md §5).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

DEFAULT_THRESHOLD = 5
DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "upstream_gate_config.json"
# "God mode": if this file is present, the integer inside it REPLACES the
# aggregated failed+error count used for the gate decision (manual override).
DEFAULT_GOD_MODE_FILE = Path(__file__).resolve().parent.parent / "god_mode.txt"

# Exit codes: 0 PASS (merge), 1 FAIL-threshold, 2 FAIL-incomplete, 3 usage error.
EXIT_PASS = 0
EXIT_FAIL_THRESHOLD = 1
EXIT_FAIL_INCOMPLETE = 2
EXIT_USAGE = 3


def _read_config_threshold(config_path: Path) -> int | None:
    """Return failure_threshold from the config file, or None if unavailable."""
    try:
        with open(config_path) as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    val = cfg.get("failure_threshold")
    if isinstance(val, bool):  # guard: bool is an int subclass
        return None
    return val if isinstance(val, int) else None


def _coerce_int(value: object) -> int | None:
    """Best-effort int coercion for env-supplied strings; None if invalid."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        s = str(value).strip()
        return int(s) if s != "" else None
    except (TypeError, ValueError):
        return None


def resolve_threshold(
    cli_threshold: int | None,
    env_value: str | None,
    config_path: Path,
    default: int = DEFAULT_THRESHOLD,
) -> tuple[int, str]:
    """Resolve the threshold and report which source supplied it.

    Returns ``(threshold, source)``. ``source`` is one of
    ``cli`` / ``env`` / ``config`` / ``default``.
    """
    if cli_threshold is not None:
        return cli_threshold, "cli"
    env_int = _coerce_int(env_value)
    if env_int is not None:
        return env_int, "env"
    cfg_int = _read_config_threshold(config_path)
    if cfg_int is not None:
        return cfg_int, "config"
    return default, "default"


def load_summary(summary_path: Path) -> dict:
    with open(summary_path) as f:
        return json.load(f)


def read_god_mode(path: Path) -> int | None:
    """Return the god-mode override for failed+error, or None if not active.

    "God mode": when ``path`` exists, its first non-blank, non-comment (``#``)
    line must be a non-negative integer. That value REPLACES the aggregated
    failed+error count for the gate decision — the deciding number comes from
    the file instead of from the test summary.

    Returns None when the file is absent, empty, or malformed (→ fall back to
    the aggregated count). A malformed file is logged, never fatal.
    """
    if not path.exists():
        return None
    try:
        raw = path.read_text()
    except OSError:
        return None
    for line in raw.splitlines():
        token = line.strip()
        if not token or token.startswith("#"):
            continue
        val = _coerce_int(token)
        if val is None or val < 0:
            print(
                f"WARNING: god_mode file {path} first value is not a non-negative "
                f"int ({token!r}); ignoring god mode.",
                file=sys.stderr,
            )
            return None
        return val
    return None


def decide(
    failed_plus_error: int,
    threshold: int,
    nodes_reported: int,
    expected_nodes: int | None,
) -> tuple[int, str]:
    """Return ``(exit_code, message)`` for the gate decision."""
    if expected_nodes is not None and nodes_reported < expected_nodes:
        return (
            EXIT_FAIL_INCOMPLETE,
            f"INCOMPLETE: {nodes_reported}/{expected_nodes} node reports present; "
            f"refusing to trust the failure count. Blocking merge.",
        )
    if failed_plus_error > threshold:
        return (
            EXIT_FAIL_THRESHOLD,
            f"FAIL: failed+error={failed_plus_error} exceeds threshold={threshold}. "
            f"Blocking merge.",
        )
    return (
        EXIT_PASS,
        f"PASS: failed+error={failed_plus_error} within threshold={threshold}. "
        f"Merge may proceed.",
    )


def _emit_github_output(**kwargs: object) -> None:
    """Write key=value pairs to $GITHUB_OUTPUT when running in CI."""
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    with open(out, "a") as f:
        for k, v in kwargs.items():
            f.write(f"{k}={v}\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--summary", type=Path, required=True, help="Path to summary.json.")
    p.add_argument(
        "--fail-threshold",
        type=int,
        default=None,
        help="Max allowed failed+error before blocking merge (overrides env/config).",
    )
    p.add_argument(
        "--expected-nodes",
        type=int,
        default=None,
        help="Expected number of node reports for the completeness check.",
    )
    p.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="Config file with failure_threshold default.",
    )
    p.add_argument(
        "--god-mode-file",
        type=Path,
        default=DEFAULT_GOD_MODE_FILE,
        help="If present, the integer inside replaces the aggregated failed+error count.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if not args.summary.exists():
        print(f"ERROR: summary not found: {args.summary}", file=sys.stderr)
        return EXIT_USAGE

    threshold, source = resolve_threshold(
        args.fail_threshold,
        os.environ.get("UPSTREAM_FAILURE_THRESHOLD"),
        args.config,
    )

    summary = load_summary(args.summary)
    failed_plus_error = int(summary.get("failed_plus_error", 0))
    nodes_reported = int(summary.get("nodes_reported", 0))

    # God mode: a present god_mode.txt forces the failed+error value.
    god_value = read_god_mode(args.god_mode_file)
    god_active = god_value is not None
    if god_active:
        print(
            f"[upstream-gate] GOD MODE active ({args.god_mode_file}): "
            f"overriding failed+error {failed_plus_error} -> {god_value}"
        )
        failed_plus_error = god_value

    exit_code, message = decide(
        failed_plus_error, threshold, nodes_reported, args.expected_nodes
    )

    verdict = "PASS" if exit_code == EXIT_PASS else "FAIL"
    print(f"[upstream-gate] threshold={threshold} (source: {source})")
    print(f"[upstream-gate] failed+error={failed_plus_error} nodes_reported={nodes_reported}")
    print(f"[upstream-gate] {message}")

    _emit_github_output(
        verdict=verdict,
        failed_plus_error=failed_plus_error,
        threshold=threshold,
        threshold_source=source,
        god_mode=str(god_active).lower(),
    )

    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a") as f:
            f.write(
                f"### Upstream gate: **{verdict}**\n\n"
                f"- failed+error: `{failed_plus_error}`"
                f"{' _(god mode override)_' if god_active else ''}\n"
                f"- threshold: `{threshold}` (source: {source})\n"
                f"- {message}\n"
            )

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
