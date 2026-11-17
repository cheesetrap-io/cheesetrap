# CI verification scopes

The required checks are `Secret scan`, `Dependency vulnerabilities`, and
`automated`. The [merge-queue policy](../../.github/merge-queue.ruleset.json)
requires all three from GitHub Actions before changes reach master. A PR's checks
and its queued candidate's checks are separate results.

## Which checks run

Secret scanning and dependency auditing run for every PR. The `acceptance`,
`browser`, `integration`, `testcontainers`, and `engines` jobs run independently on separate runners.
Each resolves the complete PR diff and uses a documentation scope only for regular
Markdown files under `docs/` or root `README.md`/`CONTRIBUTING.md`. `AGENTS.md`
and unusual paths are excluded. This scope runs operation-doc regressions,
relative links and release metadata in `acceptance`; the other jobs report the
reduced scope without building images or creating databases. This scope does not
claim `PASS_AUTOMATED`.

The required `automated` check waits for all five jobs and runs its verdict even
after an upstream failure or cancellation. It accepts only five successful job
results. A failed, cancelled, skipped, missing or unknown result fails the verdict.
Documentation jobs still execute their scope selection; they are not skipped jobs.
The jobs exchange status results, never private evidence artifacts.

The source component prepares locked npm dependencies alongside Python regressions,
joining both before Go tests. It uses `prepare-ci-web.sh --dependencies-only`
because source tests do not need Chromium. After Go unit/vet, the pinned SQL
compiler prepares alongside UI tests with its existing `-p=1` and `GOMAXPROCS=1`.
Both results are required before the UI build and source generators; a failed
compiler process, UI process, or missing/invalid UI report remains a failure.
The browser component prepares locked npm dependencies, Chromium's headless shell
and system libraries while the existing E2E harness starts its one owned
PostgreSQL stack, creates roles and migrates databases. Web preparation retains
its single-CPU Go environment; the E2E lane retains its original environment.
A fresh private completion file releases the UI build and complete Go/browser
suite only after preparation succeeds. Missing, failed, malformed, stale or
non-private status cannot supply a pass. Setup and E2E remain two required command
results, and the explicit component gate still requires successful `web-prepare`.
An installation failure never falls back to a second installation attempt. Each
lane's `Running`/`Completed` lines show its elapsed interval; overlapping intervals
must not be added as PR wait. `--prepared-web-status` accepts only a private current
acceptance status path. Standalone E2E, `--prepared-web`, and local complete
acceptance retain their preparation order. Cancellation stops owned process groups and
waits for cleanup. Local complete acceptance prepares web dependencies and Chromium
alongside Python only with `--automated-only --prepare-web`. Both Playwright
configurations use headless Chromium without a browser channel or executable
override, so `--only-shell` avoids downloading unused headed Chromium; every
existing browser test and OS dependency check still runs.

The Go integration component is a matrix with six runners: memory, jobs, retention,
API, other packages, and store. Every shard owns one fresh bounded PostgreSQL stack;
new packages enter the other-packages shard. Fail-fast is disabled so one failed
shard cannot cancel its siblings. The matrix reports success only after every
shard succeeds. The source component owns Go unit/vet, UI and all generation/drift
checks; the browser component owns every Go E2E and browser test. Each verifies
every T01–T13 test belonging to its suites;
requirements wholly owned by another component or shard are `OUT_OF_COMPONENT`.
Only an explicit component-result gate accepts `PASS_COMPONENT`; the complete
`--acceptance-results` gate rejects it. The strict `automated` join requires all
three components (including all six integration shards) and both other work jobs.
A shard result requires its explicit matching `--integration-shard` gate; the
memory shard must also report the named memory regression passing. No private
reports cross runner boundaries. Local acceptance without `--component` retains
the complete original sequence; unsharded integration retains four local batches.

All three components restore rolling Go build-object caches on top of setup-go's
dependency cache. Its key includes the OS, architecture, pinned Go version,
module files and commit. Only a successful master component saves its cache,
after its current component-result gate passes; PRs and queue candidates only
read them. Integration keys include the shard so sibling writers cannot race for
one immutable key. Integration can initially restore the other-packages, complete
integration or acceptance cache. This lets API reuse its previous runner's Go
objects on master before its own cache is seeded. Browser can restore the acceptance
cache before its own cache is seeded. A commit can refresh objects even when
the dependency cache already exists. The cache contains only `go env GOCACHE`,
never private `.local` evidence. Every test still executes with its existing
flags, including `-count=1`; cached test results do not replace checks.

Any code, configuration, lockfile, instruction, non-regular file, empty diff or
unknown comparison selects the full suite. Deletions and both sides of renames
are considered, so renaming a runtime file into a Markdown path cannot hide it.
A missing comparison commit can be fetched once; a failed fetch selects full
checks rather than a reduced pass.

Merge-queue candidates, master pushes and manual runs always execute the full
suite, including PostgreSQL, browser, generated-code, image and engine checks.
The workflow enforces this independently of the scope selector's output. A
documentation-only PR therefore still receives full candidate verification
before merge. See the [release guide](release.md) for those gates and their
limits.

## Inspect the result

The `Determine verification scope` step prints a fixed result such as
`CI_SCOPE docs documentation_only` or `CI_SCOPE full non_documentation_change`.
Each work job's summary identifies its scope. A short documentation
result is not evidence that the full automated suite ran.

| Job | Full-scope result to inspect |
| --- | --- |
| `acceptance` | Python, Go unit/vet, UI and generated-code checks; `PASS_COMPONENT_GATE: source` |
| `browser` | Every Go E2E and Chromium test; `PASS_COMPONENT_GATE: browser` |
| `integration (...)` | All six shards, including the bounded memory regression; each has `PASS_COMPONENT_GATE: integration` |
| `testcontainers` | Live loopback, pinned-image, reaper and independent-cleanup regressions |
| `engines` | Both pinned image builds and `PASS_PYTHON_IMAGE` |
| `automated` | `CI_VERDICT PASS` only after all five work jobs succeed |

For a local documentation check, run:

```sh
python3 -B scripts/test-python.py unit scripts/test_operations_docs.py
python3 -B scripts/check_links.py
scripts/release-check.sh --metadata-only
```

These commands parse and validate documentation without running its deployment
commands. They do not replace a queued candidate's complete checks.

## Measure first PR results

With an authenticated `gh` CLI, record completed native PR runs without starting
or rerunning workflows:

```sh
python3 -B scripts/ci-timings.py --sample 25:44091098271 --sample 26:44097216458
```

Repeat `--sample PR_NUMBER:RUN_ID` for each PR in the observation window. The tool
reads attempt one, checks that the selected run is the first associated PR cohort,
and rejects duplicate PRs or runs. A newer head or successful rerun cannot replace
that first result. It verifies the current 13-job inventory, native GitHub Actions
app, exact head revision, run/job URLs and required-context conclusions. Merged PRs
can lose native run-to-PR links; in that case it requires the same head repository
and branch during the PR's lifetime plus a GitHub commit-to-PR association. The
record identifies which attribution was used. Incomplete or unsupported metadata
fails collection instead of supplying a guessed zero or a partial summary.

`pr_to_required_seconds` includes the time from PR creation to workflow creation
and ends at the latest of the three required contexts' completion timestamps.
`pipeline_seconds` and `trigger_delay_seconds` show those intervals separately.
`runner_job_seconds` sums actual job intervals across the supplied first attempt,
including failed jobs. It is elapsed runner use, not a billing estimate; it does
not include queue candidates, master pushes or additional attempts. Unallocated
cancelled/skipped jobs with unknown intervals make that cost unknown.

Ordinary failed first attempts remain in the quantiles and failure count.
Documentation-only and cancelled runs retain their individual timings and costs
but are explicitly excluded from normal full-PR quantiles. Supply every normal PR
in the chosen window, including failures; the tool cannot detect omitted input PRs.
It groups results by a fingerprint of workflow, tool/image locks, harness scripts,
coverage inventory and the bounded memory fixture. Application and regression-test
code may differ within a group. This fingerprint separates CI procedures, not
identical workloads or runner hosts.

Each group reports its sample count, median and nearest-rank p95. The
`observed_targets_met` flag requires at least three eligible PRs, median below 120
seconds and p95 below 180 seconds. This describes only the supplied observations;
a small sample does not establish a long-run p95. Report the window, sample size,
failures, configuration and runner cost with any target claim. This measurement
tool does not alter verification scopes, checks, deadlines or merge-queue policy.

## Confirm the checkout repository

An Actions run's actor is the account that triggered the run. That account name
does not select the checkout repository. Inspect the checkout step's `repository`
input and `Syncing repository` line; both should name
`cheesetrap-io/cheesetrap`. The workflow uses the current repository by default
and does not pin the previous owner. Historical runs retain their original log
text after a repository transfer.
