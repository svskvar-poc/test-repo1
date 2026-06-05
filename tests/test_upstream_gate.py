"""Unit tests for the merge-queue upstream-gate POC.

Covers the three scripts:
  * gen_synthetic_reports.py — random/deterministic failure generation
  * aggregate_results.py      — per-status tally + completeness count
  * upstream_gate.py          — threshold resolution + pass/fail/incomplete decision

Plus an end-to-end generate → aggregate → gate flow.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Import the scripts under test (they live in ../scripts).
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import aggregate_results as agg  # noqa: E402
import gen_synthetic_reports as gen  # noqa: E402
import upstream_gate as gate  # noqa: E402


# ----------------------------- gen_synthetic_reports -----------------------------

def test_distribute_even_split():
    assert gen._distribute(8, 3) == [3, 3, 2]
    assert gen._distribute(6, 3) == [2, 2, 2]
    assert gen._distribute(0, 3) == [0, 0, 0]
    assert gen._distribute(5, 1) == [5]
    assert gen._distribute(10, 0) == []


def test_distribute_sums_to_total():
    for total in range(0, 20):
        for buckets in range(1, 6):
            assert sum(gen._distribute(total, buckets)) == total


def test_build_rows_failure_split_and_count():
    rows = gen._build_test_rows(total_failures=8, passing=10)
    failed = [r for r in rows if r["outcome"] == "failed"]
    errored = [r for r in rows if r["outcome"] == "error"]
    passed = [r for r in rows if r["outcome"] == "passed"]
    assert len(failed) == 4
    assert len(errored) == 4
    assert len(passed) == 10
    # failed + error == requested failures
    assert len(failed) + len(errored) == 8


def test_generate_writes_one_report_per_node(tmp_path):
    manifest = gen.generate(tmp_path, nodes=3, failures=6)
    files = sorted(tmp_path.glob("test_results_*.json"))
    assert len(files) == 3
    assert manifest["total_failures"] == 6
    assert sum(manifest["failures_per_node"]) == 6


def test_generate_random_failures_in_range(monkeypatch, tmp_path):
    # No --failures → uniform draw in [MIN_FAILURES, MAX_FAILURES].
    for seed in range(25):
        rc = gen.main(["--output-dir", str(tmp_path / f"r{seed}"), "--seed", str(seed)])
        assert rc == 0
        result = agg.aggregate(tmp_path / f"r{seed}")
        assert gen.MIN_FAILURES <= result["failed_plus_error"] <= gen.MAX_FAILURES


def test_generate_rejects_bad_args(tmp_path):
    assert gen.main(["--output-dir", str(tmp_path), "--nodes", "0"]) == 2
    assert gen.main(["--output-dir", str(tmp_path), "--failures", "-1"]) == 2


# ----------------------------- aggregate_results -----------------------------

def test_aggregate_counts_failed_plus_error(tmp_path):
    gen.generate(tmp_path, nodes=2, failures=8)
    result = agg.aggregate(tmp_path)
    assert result["failed_plus_error"] == 8
    assert result["nodes_reported"] == 2
    ts = result["total_stats"]
    assert ts["failed"] + ts["error"] == 8


def test_aggregate_skips_unreadable_report(tmp_path):
    gen.generate(tmp_path, nodes=1, failures=4)
    # Drop in a corrupt report; aggregator should warn and skip it.
    (tmp_path / "test_results_corrupt.json").write_text("{ not json")
    result = agg.aggregate(tmp_path)
    # Only the valid node's 4 failures counted; corrupt file ignored.
    assert result["failed_plus_error"] == 4
    # nodes_reported counts the files globbed (incl. corrupt name) — completeness
    # is about presence; corrupt content is surfaced via the warning + undercount.
    assert result["nodes_reported"] == 2


def test_aggregate_pass_rate():
    assert agg._pass_rate({"passed": 8, "failed": 2}) == "80.0"
    assert agg._pass_rate({"passed": 0, "failed": 0}) == "N/A"
    # skipped excluded from denom
    assert agg._pass_rate({"passed": 5, "skipped": 100}) == "100.0"


def test_aggregate_missing_dir_returns_1(tmp_path):
    assert agg.main([str(tmp_path / "does-not-exist")]) == 1


def test_aggregate_writes_summary(tmp_path):
    gen.generate(tmp_path, nodes=2, failures=3)
    out = tmp_path / "summary.json"
    rc = agg.main([str(tmp_path), "--summary-out", str(out)])
    assert rc == 0
    data = json.loads(out.read_text())
    assert data["failed_plus_error"] == 3
    assert data["nodes_reported"] == 2


# ----------------------------- upstream_gate: threshold resolution -----------------------------

def test_resolve_threshold_cli_wins(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"failure_threshold": 9}))
    val, src = gate.resolve_threshold(3, "7", cfg)
    assert (val, src) == (3, "cli")


def test_resolve_threshold_env_over_config(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"failure_threshold": 9}))
    val, src = gate.resolve_threshold(None, "7", cfg)
    assert (val, src) == (7, "env")


def test_resolve_threshold_config_over_default(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"failure_threshold": 9}))
    val, src = gate.resolve_threshold(None, None, cfg)
    assert (val, src) == (9, "config")


def test_resolve_threshold_default_when_all_absent(tmp_path):
    missing = tmp_path / "nope.json"
    val, src = gate.resolve_threshold(None, None, missing)
    assert (val, src) == (gate.DEFAULT_THRESHOLD, "default")


def test_resolve_threshold_blank_env_falls_through(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"failure_threshold": 4}))
    # Unset repo variable surfaces as "" — must fall through to config.
    val, src = gate.resolve_threshold(None, "", cfg)
    assert (val, src) == (4, "config")


def test_coerce_int_edge_cases():
    assert gate._coerce_int("5") == 5
    assert gate._coerce_int("  6 ") == 6
    assert gate._coerce_int(7) == 7
    assert gate._coerce_int("") is None
    assert gate._coerce_int("abc") is None
    assert gate._coerce_int(None) is None
    assert gate._coerce_int(True) is None  # bool guard


# ----------------------------- upstream_gate: decision -----------------------------

def test_decide_pass_within_threshold():
    code, msg = gate.decide(failed_plus_error=5, threshold=5, nodes_reported=3, expected_nodes=3)
    assert code == gate.EXIT_PASS
    assert "PASS" in msg


def test_decide_fail_over_threshold():
    code, msg = gate.decide(failed_plus_error=6, threshold=5, nodes_reported=3, expected_nodes=3)
    assert code == gate.EXIT_FAIL_THRESHOLD
    assert "FAIL" in msg


def test_decide_boundary_equal_passes():
    # ">" semantics: exactly at threshold must PASS.
    code, _ = gate.decide(failed_plus_error=5, threshold=5, nodes_reported=1, expected_nodes=1)
    assert code == gate.EXIT_PASS


def test_decide_incomplete_blocks_even_if_under_threshold():
    code, msg = gate.decide(failed_plus_error=0, threshold=5, nodes_reported=2, expected_nodes=3)
    assert code == gate.EXIT_FAIL_INCOMPLETE
    assert "INCOMPLETE" in msg


def test_decide_no_expected_nodes_skips_completeness():
    code, _ = gate.decide(failed_plus_error=0, threshold=5, nodes_reported=0, expected_nodes=None)
    assert code == gate.EXIT_PASS


# ----------------------------- end-to-end -----------------------------

def _run_gate(summary_path, expected_nodes, env_threshold=None, monkeypatch=None):
    argv = ["--summary", str(summary_path), "--expected-nodes", str(expected_nodes)]
    if env_threshold is not None:
        monkeypatch.setenv("UPSTREAM_FAILURE_THRESHOLD", str(env_threshold))
    return gate.main(argv)


def test_end_to_end_pass(tmp_path, monkeypatch):
    monkeypatch.delenv("UPSTREAM_FAILURE_THRESHOLD", raising=False)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    results = tmp_path / "results"
    gen.generate(results, nodes=3, failures=4)  # 4 <= 5 → PASS
    summary = results / "summary.json"
    agg.main([str(results), "--summary-out", str(summary)])
    no_god = tmp_path / "no_god.txt"
    assert gate.main(
        ["--summary", str(summary), "--expected-nodes", "3", "--god-mode-file", str(no_god)]
    ) == gate.EXIT_PASS


def test_end_to_end_fail_over_threshold(tmp_path, monkeypatch):
    monkeypatch.delenv("UPSTREAM_FAILURE_THRESHOLD", raising=False)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    results = tmp_path / "results"
    gen.generate(results, nodes=3, failures=8)  # 8 > 5 → FAIL
    summary = results / "summary.json"
    agg.main([str(results), "--summary-out", str(summary)])
    no_god = tmp_path / "no_god.txt"
    assert gate.main(
        ["--summary", str(summary), "--expected-nodes", "3", "--god-mode-file", str(no_god)]
    ) == gate.EXIT_FAIL_THRESHOLD


def test_end_to_end_env_threshold_override(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    results = tmp_path / "results"
    gen.generate(results, nodes=3, failures=4)  # 4 failures
    summary = results / "summary.json"
    agg.main([str(results), "--summary-out", str(summary)])
    # Tighten threshold via env to 3 → 4 > 3 → FAIL.
    monkeypatch.setenv("UPSTREAM_FAILURE_THRESHOLD", "3")
    no_god = tmp_path / "no_god.txt"
    assert gate.main(
        ["--summary", str(summary), "--expected-nodes", "3", "--god-mode-file", str(no_god)]
    ) == gate.EXIT_FAIL_THRESHOLD


def test_gate_missing_summary_usage_error(tmp_path):
    assert gate.main(["--summary", str(tmp_path / "nope.json")]) == gate.EXIT_USAGE


# ----------------------------- upstream_gate: god mode -----------------------------

def test_read_god_mode_absent_returns_none(tmp_path):
    assert gate.read_god_mode(tmp_path / "god_mode.txt") is None


def test_read_god_mode_plain_int(tmp_path):
    f = tmp_path / "god_mode.txt"
    f.write_text("7\n")
    assert gate.read_god_mode(f) == 7


def test_read_god_mode_skips_comments_and_blanks(tmp_path):
    f = tmp_path / "god_mode.txt"
    f.write_text("# a comment\n\n   \n3\n")
    assert gate.read_god_mode(f) == 3


def test_read_god_mode_malformed_ignored(tmp_path):
    f = tmp_path / "god_mode.txt"
    f.write_text("# only comments, no number\n")
    assert gate.read_god_mode(f) is None
    bad = tmp_path / "bad.txt"
    bad.write_text("not-a-number\n")
    assert gate.read_god_mode(bad) is None
    neg = tmp_path / "neg.txt"
    neg.write_text("-4\n")
    assert gate.read_god_mode(neg) is None


def _make_summary(tmp_path, failures, nodes):
    results = tmp_path / "results"
    gen.generate(results, nodes=nodes, failures=failures)
    summary = results / "summary.json"
    agg.main([str(results), "--summary-out", str(summary)])
    return summary


def test_god_mode_forces_fail_over_real_pass(tmp_path, monkeypatch):
    monkeypatch.delenv("UPSTREAM_FAILURE_THRESHOLD", raising=False)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    summary = _make_summary(tmp_path, failures=2, nodes=3)  # real → PASS
    god = tmp_path / "god_mode.txt"
    god.write_text("9\n")  # override → 9 > 5 → FAIL
    rc = gate.main(
        ["--summary", str(summary), "--expected-nodes", "3", "--god-mode-file", str(god)]
    )
    assert rc == gate.EXIT_FAIL_THRESHOLD


def test_god_mode_forces_pass_over_real_fail(tmp_path, monkeypatch):
    monkeypatch.delenv("UPSTREAM_FAILURE_THRESHOLD", raising=False)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    summary = _make_summary(tmp_path, failures=8, nodes=3)  # real → FAIL
    god = tmp_path / "god_mode.txt"
    god.write_text("1\n")  # override → 1 <= 5 → PASS
    rc = gate.main(
        ["--summary", str(summary), "--expected-nodes", "3", "--god-mode-file", str(god)]
    )
    assert rc == gate.EXIT_PASS


def test_god_mode_absent_uses_real_count(tmp_path, monkeypatch):
    monkeypatch.delenv("UPSTREAM_FAILURE_THRESHOLD", raising=False)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    summary = _make_summary(tmp_path, failures=8, nodes=3)  # real → FAIL
    rc = gate.main(
        [
            "--summary",
            str(summary),
            "--expected-nodes",
            "3",
            "--god-mode-file",
            str(tmp_path / "absent.txt"),
        ]
    )
    assert rc == gate.EXIT_FAIL_THRESHOLD
