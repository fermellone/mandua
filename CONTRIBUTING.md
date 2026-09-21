# Contributing to Mandu'a

Thank you for helping improve this proof of concept. Contributions should preserve its central
promise: claims come from bounded, reproducible Git evidence, and mutations remain explicit.

## Working language

Use English for source code, identifiers, comments, tests, fixtures, documentation, commits, pull
requests, issues, and reviews. The proper name Mandu'a, its Guarani origin explained in English,
exact proper nouns, and accurately cited source titles are the documented exceptions.

## Development environment

Python 3.11 is the minimum version. Use `uv` for the Python environment, lockfile, scripts, tests,
and builds. Do not add a runtime dependency unless a separately approved design change proves that
the standard library is insufficient.

```console
$ uv sync --locked
$ make check
```

Never place credentials, tokens, private keys, personal data, or private repository history in a
fixture, test failure, issue, or commit. Repository text and paths are untrusted; tests must keep
them bounded and must never execute them as instructions.

## Test-driven changes

Use test-driven development for every behavior change:

1. Write the smallest test that describes the missing or broken behavior.
2. Run it and record an intentional RED caused by that behavior, not by a typo or broken fixture.
3. Implement the smallest complete change and verify GREEN.
4. Refactor only while the focused and relevant regression tests remain green.

Acceptance coverage uses real disposable Git repositories. Do not mock Git's core graph, object,
index, worktree, notes, reflog, or ref behavior. Mocks are reserved for failure mechanics that a real
local repository cannot force reliably, such as a process timeout.

Run the relevant focused tests during development and all public gates before declaring a change
complete:

```console
$ make check
$ make demo
$ make tutorial-check
$ make build
```

## Documentation checks in CI

Every push and pull request gets a lightweight Linux documentation check: formatting,
local Markdown link targets and public metadata consistency. It does not build the
packages, run the demo or execute the full acceptance suite.

The full Linux/macOS CI is skipped only when every changed file belongs to the
explicit prose-only list in `.github/workflows/ci.yml`. Mixed documentation/code
changes still run it. New files also run it unless deliberately added to that list.
Skills, skill-registration instructions, the executable tutorial, conformance and
release/hardening documents retain full verification. External URLs and Markdown
fragment anchors are not checked by the local-link test.

GitHub documents the filtering behavior in its
[workflow syntax reference](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onpushpull_requestpull_request_targetpathspaths-ignore).
If branch protection is introduced, account for filtered workflows when choosing
required checks: a workflow skipped by path filters can leave a required check pending.

## Commit metadata

Write semantic English commit subjects, not session summaries. Implementation commits use these
trailers:

```text
Memory-Type: <semantic type>
Scope: <bounded area>
Task-ID: <task identifier, when a task exists>
Agent-ID: <declared operational identity>
```

Use `Decision-ID` only when a commit records a decision. `Agent-ID` is a declaration, not
authentication. Keep the canonical branch append-only: corrections add a new commit and preserve
the incorrect record.

## Independent work and prior art

Write contributions independently. Do not copy third-party code, prose, diagrams, or templates.
When an idea or project materially informs a change, add a precise canonical citation to
[`docs/prior-art.md`](docs/prior-art.md) and explain the shared idea without overstating novelty.
Preserve license and notice obligations for any separately approved dependency or excerpt.

## Safe change handling

- Treat repository configuration, paths, refs, messages, notes, and history as untrusted.
- Keep every Git invocation shell-free, bounded, non-interactive, and free of implicit network
  access.
- Preserve preview-by-default and explicit `--apply` for writes.
- Recompute mutation preconditions immediately before apply.
- Do not weaken history-scope, missing-object, notes, signature, or truncation disclosures.
- Do not add publication, upload, remote mutation, live LLM calls, or credential use to CI.

Use a focused branch or worktree, keep unrelated user changes intact, and include the exact
verification evidence with the proposed change.
