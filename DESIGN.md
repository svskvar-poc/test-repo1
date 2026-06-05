# POC: Pre-merge `upstream-tests` via Merge Queue + Failure-Threshold Gate

Proof-of-concept that converts the **post-merge** upstream test suite (from
the upstream repo) into a **pre-merge gate** that runs inside a
GitHub **merge queue**, aggregates a single pass/fail summary across all tests,
and **blocks the merge** when the number of failed tests crosses a configurable
threshold — with an explicit **bypass** path.

> This repo is a standalone POC. It mirrors the real pipeline's shape but runs
> the test step as a **simulation** on free `ubuntu-latest` runners (synthetic
> pytest-JSON reports) so the gate logic can be exercised end-to-end without
> trn2 hardware. Mapping back to the real workflows is noted throughout.

---

## 1. What exists today (in the upstream repo) — the starting point

| Piece | File | Behavior today |
|-------|------|----------------|
| Post-merge trigger | `.github/workflows/merge.yml` | `on: push: branches:[main]` → runs **after** merge |
| Reusable suite | `.github/workflows/_upstream-tests.yml` | matrix over ~26–28 trn2 nodes, `fail-fast:false`, then aggregate |
| Aggregator | `tests/.../runner/aggregate_upstream_results.py` | Produces summary + **failures count** (`failed`+`error`), but **only reports** — never blocks (always exit 0) |
| PR gates | `.github/workflows/pull.yml` | Existing pre-merge unit tests (model for pre-merge wiring) |

Key facts this POC relies on (to be confirmed by the validation gate):
- The aggregator already computes a failure count and per-status totals — we add
  a **threshold decision** on top of it; we do not reinvent counting.
- The suite matrix is `fail-fast:false` and dynamic — a completeness check matters
  (all matrix nodes reported) before trusting the count.

---

## 2. Target design (the POC)

```
  PR author clicks "Merge when ready"
              │
              ▼
   ┌──────────────────────────┐
   │  GitHub Merge Queue       │  length = 1 (configurable; pinned to 1 now)
   │  forms temp branch &       │  → emits `merge_group` event
   │  emits merge_group         │
   └────────────┬─────────────┘
                ▼
   ┌──────────────────────────────────────────────┐
   │ workflow: merge-queue-upstream.yml             │
   │  on: merge_group                               │
   │   1. run upstream-tests (sim: N failures)      │
   │   2. aggregate → one summary for ALL tests     │
   │   3. gate: failed > THRESHOLD ?  → fail check   │
   │      (unless BYPASS active)                     │
   └────────────┬───────────────────────────────────┘
                ▼
   Required status check result → queue merges (pass) or drops PR (fail)
```

Three knobs the user asked for, and **where each lives**:

| Knob | Default | Where it's set | Changeable without code edit? |
|------|---------|----------------|-------------------------------|
| **Failure threshold** | `5` | Actions **repo variable** `UPSTREAM_FAILURE_THRESHOLD` (primary), with config-file + hardcoded fallback | ✅ yes (Settings UI / `gh variable set`) |
| **Merge queue length** | `1` | **Repository ruleset** `merge_queue` params (`max_entries_to_merge` / `max_entries_to_build`) | ✅ yes (Settings UI / `gh api`) |
| **Bypass** | off | Native **admin break-glass** via ruleset `bypass_actors` (no gate code) | ✅ yes (admin merges despite failing check) |

---

## 3. How to make the **threshold (5)** a variable — recommendation

You asked specifically how to make `5` modifiable. Recommended approach, in
priority order (the gate script resolves the first one that is set):

1. **GitHub Actions repository variable `UPSTREAM_FAILURE_THRESHOLD`** *(primary)*
   - Set in **Settings → Secrets and variables → Actions → Variables**, or:
     ```bash
     gh variable set UPSTREAM_FAILURE_THRESHOLD --body 5 --repo <owner>/<repo>
     ```
   - Referenced in the workflow and passed to the gate:
     ```yaml
     env:
       UPSTREAM_FAILURE_THRESHOLD: ${{ vars.UPSTREAM_FAILURE_THRESHOLD }}
     ```
   - **No code change, no PR, no redeploy** — takes effect on the next queue run.
   - Can also be scoped per-**Environment** (e.g. stricter on `production`).

2. **Committed config file `config/upstream_gate_config.json`** *(default of record)*
   ```json
   { "failure_threshold": 5, "merge_queue_length": 1 }
   ```
   - Auditable via PR/git history; used when the repo variable is unset.

3. **Hardcoded fallback (`5`) in `scripts/upstream_gate.py`** — last resort so the
   gate never crashes on misconfiguration.

Resolution order in code: `--fail-threshold` CLI arg → `UPSTREAM_FAILURE_THRESHOLD`
env → config file → hardcoded `5`.

**Why a repo variable over alternatives**
- `workflow_dispatch` input: doesn't apply — the merge-queue run is automatic
  (`merge_group`), not manually dispatched.
- Bare `env:` in YAML: works but requires a PR to change; less "operator-friendly".
- Repo variable wins because an operator can retune the gate live, and the
  config-file fallback keeps a versioned default.

---

## 4. How to make the **merge queue length** configurable (pinned to 1)

Merge-queue sizing is **not** in a workflow file — it lives in the branch's
**ruleset** (or branch-protection) merge-queue settings. To pin length = 1 while
keeping it changeable, set these `merge_queue` rule parameters (see
`ruleset/main-merge-queue.json`):

```jsonc
{
  "type": "merge_queue",
  "parameters": {
    "merge_method": "SQUASH",          // or MERGE / REBASE
    "max_entries_to_build": 1,          // build 1 PR at a time
    "max_entries_to_merge": 1,          // merge 1 PR at a time  → "length 1"
    "min_entries_to_merge": 1,
    "min_entries_to_merge_wait_minutes": 0,
    "check_response_timeout_minutes": 60,
    "grouping_strategy": "ALLGREEN"
  }
}
```

Apply/update via the REST rulesets API (operator action, no code edit):
```bash
gh api -X POST /repos/<owner>/<repo>/rulesets --input ruleset/main-merge-queue.json
# later, to change the length, bump max_entries_to_* and PUT the ruleset by id.
```
To raise the length later, change `max_entries_to_merge` / `max_entries_to_build`.

> Exact `merge_queue` parameter names are per the current GitHub repository
> rulesets API and should be confirmed against the live API before relying on
> them in production (validation gate). The POC treats this file as the single
> source of truth for queue sizing.

---

## 5. How the **bypass** works  → **native admin break-glass** (chosen)

The bypass is **GitHub-native**, not implemented in the gate code:

- Users on the ruleset **bypass list** (or repo admins with the "merge without
  waiting for requirements / bypass branch protections" permission) can force the
  merge even when the upstream-tests required check is failing.
- Configured in the ruleset's **bypass list** (`bypass_actors`) — see
  `ruleset/main-merge-queue.json`. The action is recorded in the PR timeline /
  audit log.

Because the bypass is handled by GitHub at merge time, `scripts/upstream_gate.py`
stays simple: it only decides pass/fail on completeness + threshold and never has
to know about overrides. There is **no label or env bypass** in the gate.

---

## 6. Gate decision (the core logic)

`scripts/upstream_gate.py` is the single decision point:

```
failed_count = failed + error   (from the aggregated summary)
god mode     : if god_mode.txt is present, its integer REPLACES failed_count
threshold    = resolve(arg, env, config, default=5)

if not all matrix nodes reported   -> FAIL (incomplete; don't trust the count)
elif failed_count > threshold      -> FAIL (block merge)
else                               -> PASS
```

**God mode override.** If a `god_mode.txt` file is present at the repo root, the
first non-comment integer inside it **replaces** the aggregated `failed + error`
count for the decision — the deciding number comes from the file, not the test
run. The threshold comparison still applies (value > threshold ⇒ blocked).
Delete the file (or its number) to return to normal aggregation. Malformed/empty
files are ignored with a warning (never fatal).

Exit code 0 = check passes (queue merges); non-zero = check fails (PR dropped from
queue). This exit code is what GitHub records as the required status check result.
Bypass is **not** handled here — see §5 (native admin break-glass).

---

## 7. Files in this POC

| Path | Purpose |
|------|---------|
| `.github/workflows/merge-queue-upstream.yml` | `on: merge_group` — runs sim suite → aggregate → gate |
| `.github/workflows/pr-checks.yml` | `on: pull_request` — lightweight check so status shows on PR |
| `scripts/gen_synthetic_reports.py` | Emit synthetic pytest-JSON reports with N failures (stand-in for trn2 run) |
| `scripts/aggregate_results.py` | Minimal aggregator: count failed/error, emit summary (mirrors real schema) |
| `scripts/upstream_gate.py` | Threshold + bypass + completeness decision → exit code |
| `config/upstream_gate_config.json` | Versioned defaults (threshold, queue length) |
| `god_mode.txt` | Optional override: if present, its integer replaces the failed+error count |
| `ruleset/main-merge-queue.json` | Repo ruleset: require merge queue (length 1) + required check |
| `tests/test_upstream_gate.py` | Unit tests for the gate decision logic |

---

## 8. Resolved choices

- **Merge method**: `SQUASH`.
- **Bypass**: native admin break-glass only (ruleset `bypass_actors`); no gate code.
- **Test step**: simulated — the synthetic generator emits a **random** failure
  count in **[1, 8]** per run (with threshold 5, runs land on both sides of the gate).
- **Threshold semantics**: `failed + error` (matches the real aggregator's
  `FAILURE_STATUSES`).
