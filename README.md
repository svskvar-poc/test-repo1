# e12 — Pre-merge upstream-tests via Merge Queue (POC)

Proof-of-concept that turns the **post-merge** upstream test suite into a
**pre-merge gate** running inside a GitHub **merge queue**, with a configurable
**failure threshold** and a native-admin **bypass**. See `DESIGN.md` for the full
rationale and how it maps back to the upstream repo.

## The flow

```
PR author clicks "Merge when ready"
   → GitHub merge queue (length 1, squash)
   → merge_group event
   → .github/workflows/merge-queue-upstream.yml
        simulate upstream run (random 1–8 failures)
        → aggregate one summary for ALL tests
        → gate: failed+error > threshold ? block : merge
```

## Run it locally

```bash
# 1) simulate a run (omit --failures for a random 1–8; --seed for repeatability)
python3 scripts/gen_synthetic_reports.py --output-dir /tmp/up --nodes 3 --failures 7

# 2) aggregate one summary across all nodes
python3 scripts/aggregate_results.py /tmp/up --summary-out /tmp/up/summary.json

# 3) gate (exit 0 = merge may proceed, non-zero = blocked)
python3 scripts/upstream_gate.py --summary /tmp/up/summary.json --expected-nodes 3
echo "exit=$?"
```

Run the tests:

```bash
python3 -m pytest tests/ -v
```

## The three knobs

| Knob | Default | Change it without editing code |
|------|---------|-------------------------------|
| **Failure threshold** | `5` | Actions repo variable: `gh variable set UPSTREAM_FAILURE_THRESHOLD --body 5` (or Settings → Variables). Falls back to `config/upstream_gate_config.json`, then built-in `5`. |
| **Merge queue length** | `1` | Edit `merge_queue.max_entries_to_merge` / `max_entries_to_build` in `ruleset/main-merge-queue.json`, re-apply via `gh api`. |
| **Bypass** | off | Native admin break-glass — admins / `bypass_actors` in the ruleset merge despite a failing check. |
| **God mode** | off | Create `god_mode.txt` at the repo root with an integer — it **replaces** the aggregated failed+error count for the decision. Delete it to disable. |

### Apply the ruleset (enables the merge queue + required check)

```bash
gh api -X POST /repos/<owner>/<repo>/rulesets \
  --input ruleset/main-merge-queue.json
```

> Notes to verify against the live GitHub API before production use:
> - `merge_queue` parameter names (`max_entries_to_merge`, etc.).
> - `bypass_actors[].actor_id` — `5` is used here for the built-in Repository
>   admin role; confirm the ID for your org.
> - The `required_status_checks` context string must match the job name
>   (`upstream-gate (pre-merge)`).

## Layout

```
.github/workflows/
  merge-queue-upstream.yml   # on: merge_group — the real gate
  pr-checks.yml              # on: pull_request — fast unit tests
scripts/
  gen_synthetic_reports.py   # stand-in for the trn2 run (random 1–8 failures)
  aggregate_results.py       # one summary for all tests (failed+error count)
  upstream_gate.py           # threshold + completeness decision → exit code
config/upstream_gate_config.json   # versioned defaults
god_mode.txt                       # optional override: integer replaces failed+error
ruleset/main-merge-queue.json      # require merge queue (len 1, squash) + bypass
tests/test_upstream_gate.py        # unit + end-to-end tests
```
