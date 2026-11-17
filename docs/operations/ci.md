# CI verification scopes

The required checks are `Secret scan`, `Dependency vulnerabilities`, and
`automated`. The [merge-queue policy](../../.github/merge-queue.ruleset.json)
requires all three from GitHub Actions before changes reach master. A PR's checks
and its queued candidate's checks are separate results.

## Which checks run

Secret scanning and dependency auditing run for every PR. The `acceptance`,
`integration`, `testcontainers`, and `engines` jobs run independently on separate runners.
Each resolves the complete PR diff and uses a documentation scope only for regular
Markdown files under `docs/` or root `README.md`/`CONTRIBUTING.md`. `AGENTS.md`
and unusual paths are excluded. This scope runs operation-doc regressions,
relative links and release metadata in `acceptance`; the other jobs report the
reduced scope without building images or creating databases. This scope does not
claim `PASS_AUTOMATED`.

The required `automated` check waits for all four jobs and runs its verdict even
after an upstream failure or cancellation. It accepts only four successful job
results. A failed, cancelled, skipped, missing or unknown result fails the verdict.
Documentation jobs still execute their scope selection; they are not skipped jobs.
The jobs exchange status results, never private evidence artifacts.

The source component prepares locked npm dependencies and Chromium alongside Python
regressions, joining both before Go tests and DB work. Setup is a required command
in the private result. A failed setup stops acceptance before E2E can install
again. Cancellation stops both owned process groups and waits for cleanup.
Local acceptance prepares web dependencies this way only when explicitly invoked
with `--automated-only --prepare-web`.

The Go integration component is a five-runner matrix: memory, jobs, retention,
other packages, and store. Every shard owns one fresh bounded PostgreSQL stack;
new packages enter the other-packages shard. Fail-fast is disabled so one failed
shard cannot cancel its siblings. The matrix reports success only after every
shard succeeds. The source component owns Go unit/vet, E2E/browser, UI and all
generation/drift checks. Each verifies every T01–T13 test belonging to its suites;
requirements wholly owned by another component or shard are `OUT_OF_COMPONENT`.
Only an explicit component-result gate accepts `PASS_COMPONENT`; the complete
`--acceptance-results` gate rejects it. The strict `automated` join requires both
components (including all five integration shards) and both other work jobs.
A shard result requires its explicit matching `--integration-shard` gate; the
memory shard must also report the named memory regression passing. No private
reports cross runner boundaries. Local acceptance without `--component` retains
the complete original sequence; unsharded integration retains four local batches.

Both components restore rolling Go build-object caches on top of setup-go's
dependency cache. Its key includes the OS, architecture, pinned Go version,
module files and commit. Only a successful master component saves its cache,
after its current component-result gate passes; PRs and queue candidates only
read them. Integration keys include the shard so sibling writers cannot race for
one immutable key. Each can initially restore the complete integration or
acceptance cache from master before its own cache is seeded. A commit can refresh objects even when
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
| `acceptance` | Python, Go unit/vet, E2E/browser, UI and generated-code checks; `PASS_COMPONENT_GATE: source` |
| `integration (...)` | All five shards, including the bounded memory regression; each has `PASS_COMPONENT_GATE: integration` |
| `testcontainers` | Live loopback, pinned-image, reaper and independent-cleanup regressions |
| `engines` | Both pinned image builds and `PASS_PYTHON_IMAGE` |
| `automated` | `CI_VERDICT PASS` only after all four work jobs succeed |

For a local documentation check, run:

```sh
python3 -B scripts/test-python.py unit scripts/test_operations_docs.py
python3 -B scripts/check_links.py
scripts/release-check.sh --metadata-only
```

These commands parse and validate documentation without running its deployment
commands. They do not replace a queued candidate's complete checks.

## Confirm the checkout repository

An Actions run's actor is the account that triggered the run. That account name
does not select the checkout repository. Inspect the checkout step's `repository`
input and `Syncing repository` line; both should name
`cheesetrap-io/cheesetrap`. The workflow uses the current repository by default
and does not pin the previous owner. Historical runs retain their original log
text after a repository transfer.
