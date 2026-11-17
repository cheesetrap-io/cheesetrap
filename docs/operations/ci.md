# CI verification scopes

The required checks are `Secret scan`, `Dependency vulnerabilities`, and
`automated`. The [merge-queue policy](../../.github/merge-queue.ruleset.json)
requires all three from GitHub Actions before changes reach master. A PR's checks
and its queued candidate's checks are separate results.

## Which checks run

Secret scanning and dependency auditing run for every PR. The `acceptance`,
`testcontainers`, and `engines` jobs run independently on separate runners.
Each resolves the complete PR diff and uses a documentation scope only for regular
Markdown files under `docs/` or root `README.md`/`CONTRIBUTING.md`. `AGENTS.md`
and unusual paths are excluded. This scope runs operation-doc regressions,
relative links and release metadata in `acceptance`; the other jobs report the
reduced scope without building images or creating databases. This scope does not
claim `PASS_AUTOMATED`.

The required `automated` check waits for all three jobs and runs its verdict even
after an upstream failure or cancellation. It accepts only three successful job
results. A failed, cancelled, skipped, missing or unknown result fails the verdict.
Documentation jobs still execute their scope selection; they are not skipped jobs.
The jobs exchange status results, never private evidence artifacts.

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
| `acceptance` | Python, Go, disposable DB, browser, UI and generated-code checks; current `PASS_AUTOMATED_GATE` |
| `testcontainers` | Live loopback, pinned-image, reaper and independent-cleanup regressions |
| `engines` | Both pinned image builds and `PASS_PYTHON_IMAGE` |
| `automated` | `CI_VERDICT PASS` only after all three work jobs succeed |

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
