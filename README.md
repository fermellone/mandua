# Mandu'a

Verifiable agent memory, backed by Git.

**[Try the demo with your agent](docs/try-demo.md)** - Register the skill, prepare
the fictional garden memory, and start a conversation. For a less biased trial,
use a clean client environment; the guide explains options, including a separate
profile, a Docker container, or a virtual machine.

Mandu'a means to remember in Guarani. It is a deterministic reference implementation of a
strict, Git-evidence-backed memory model for agents working in existing repositories. Current
knowledge remains in ordinary files; transitions, recorded decisions, alternatives, reviews,
corrections, and recovery evidence remain in the repository's native Git history.

This repository is an open-source proof of concept, not a production-ready service. The core uses
Python and Git without an LLM, vector database, memory database, API key, or network service.
Mandu'a does not claim that Git-backed memory is a new general idea. Its narrower goal is to make
one evidence-oriented interpretation executable, testable, and reproducible.

## Requirements

- Python 3.11 or newer.
- Git with the capabilities exercised by the local checks. Git 2.50.1 is the observed development
  baseline; Mandu'a detects required capabilities instead of depending on that exact version.
- [uv](https://docs.astral.sh/uv/) for environments, locking, tests, and builds.
- Linux or macOS for the validated workflows. Windows has not been validated.

The project has no runtime Python dependencies. Dependency installation may require network access
the first time `uv sync --locked` resolves the development and build tools. The deterministic demo
itself uses only local file transport and makes no network call.

The repository also contains an optional, experimental Jev adapter under
[`adapters/jev`](adapters/jev/README.md). The Mandu'a core and demo remain deterministic and
offline; the adapter uses its local mock by default and only calls TypeSafe AI when
`TYPESAFE_API_KEY` is explicitly configured. That external integration is outside the core
security and reproducibility claims of version 0.1.0.

The canonical public repository is hosted at [https://github.com/fermellone/mandua](https://github.com/fermellone/mandua). The commands below are
for an existing local source checkout or freshly cloned repository.

## 90-second local demo

From the repository root:

```console
$ uv sync --locked
$ make demo
```

`make demo` builds a disposable fictional community-garden history, runs the public memory
operations against real Git objects, configures a local bare remote, transfers the review-notes ref
explicitly, and verifies a local bundle. It uses no credentials, model, external host, or database.
The console prints the retained output, report, and bundle paths plus the observed network status;
the retained JSON report records every verified story claim and the bundle-verification result.
Ninety seconds is a walkthrough target, not a performance guarantee for every machine.

For the same story as visible commands and explanatory evidence, read
[`docs/tutorial.md`](docs/tutorial.md), then verify that the generated document and its 69 commands
still agree with the versioned scenario:

```console
$ make tutorial-check
```

The complete 18-case ownership map is in [`docs/conformance.md`](docs/conformance.md). Run its
focused executable gate with `make conformance-check`.

## Talk to the demo memory (experimental)

Follow the [demo walkthrough](docs/try-demo.md): register the skill with a message
to your client, ask it to prepare the demo, read
[the community garden's story](docs/demo-story.md), and have a conversation.
No terminal commands are needed in the walkthrough. This remains a proof of
concept for exploring a fictional memory, not a production memory service.

The conversational workflow has been tried in pi and in local Codex trials.
The general repository-selection flow and agent view were exercised in Codex with
a fictional library, including reports outside the policy file; see the
[results and limits](docs/agent-validation.md).
Claude Code has not yet been verified end to end. The skill uses your client's
existing model; evidence may be sent to that model's provider. No additional model
API key is required. Exploration is read-only after demo creation.

The skill can also query a memory repository selected by its absolute path,
without preparing the garden. That selection belongs to the conversation;
registration does not choose a default memory or enable automatic ingestion.

## Command shape

Repository operations use this form:

```text
mandua --repo <path> <operation> [arguments] [--format human|json|agent]
```

`human` is the default format. `json` is the stable adapter boundary. `agent` is a
deterministic conversational view with citable records and explicit retrieval limits;
it does not use another model. `demo` is the only operation that does not accept
`--repo`, and supports only `human` and `json`.

For example, `decision ID --format agent` retrieves decision metadata and review
notes, not file contents. Even an untruncated lookup cannot establish that the
repository contains no measurements. The agent view separates these limits from
evidence and omits low-level history counters and empty per-claim citation lists.
Use `--format json` for the original diagnostic fields; existing integrations keep
their contract.

| Operation | Verified behavior |
| --- | --- |
| `status` | Reports the working tree, index, branch, reachable history scope, notes availability, and incomplete-history warnings. |
| `demo` | Builds and verifies the deterministic local community-garden scenario. |
| `context` | Reconstructs bounded context for an exact task trailer, a branch delta, or the current checkout. |
| `timeline` | Returns bounded chronological history for an optional path and Git revision range. |
| `why` | Uses blame, commit metadata, and fetched review notes to explain a line without inventing an absent reason. |
| `origin` | Finds exact text additions and removals, including text absent from `HEAD`. |
| `evolution` | Follows one repository-relative path through bounded rename and deletion history. |
| `compare` | Compares two immutable tips through their merge base, exclusive commits, state diff, and carefully labeled patch correspondence. |
| `decision` | Finds an exact `Decision-ID`, related review notes, and graph-verified correction links. |
| `recover` | Finds bounded local reflog and reference candidates and can create one explicit recovery branch. |
| `checkpoint` | Previews or records one semantic commit from explicit paths or an already prepared index. |
| `annotate` | Previews or appends bounded review metamemory under `refs/notes/review`. |
| `integrate` | Previews with Git merge evidence, validates policy, and can create a non-fast-forward integration commit. |
| `correct` | Previews or appends a canonical correction while preserving the incorrect commit as queryable history. |

See the live argument surface at any time:

```console
$ uv run mandua --help
$ uv run mandua checkpoint --help
```

## Stable `MemoryResult` boundary

Every repository operation renders the same versioned object in human or JSON form. This is a
representative `status` result; repository-specific OIDs, refs, counts, evidence, and warnings vary.

<!-- memory-result-example -->
```json
{
  "answer": "Repository status and reachable history scope were inspected.",
  "applied": false,
  "changes": [],
  "confidence": "high",
  "evidence": [],
  "gaps": [],
  "history_scope": {
    "commit_count": 1,
    "end_oid": "0123456789abcdef0123456789abcdef01234567",
    "missing_objects": [],
    "notes_available": false,
    "refs": [
      "HEAD",
      "refs/heads/main"
    ],
    "shallow": false,
    "start_oid": "0123456789abcdef0123456789abcdef01234567",
    "truncated": false
  },
  "inferred": [],
  "observed": [
    {
      "evidence": [],
      "text": "Repository state was read without refreshing the index or contacting a remote."
    }
  ],
  "operation": "status",
  "schema_version": "1.0",
  "warnings": [
    "Review notes are unavailable."
  ]
}
```

The same object keeps recorded observations separate from inferences, names evidence needed to
reproduce a claim, reports gaps and warnings, and discloses the reachable history scope.

## Read examples

```console
$ uv run mandua --repo /path/to/repository status --format json
$ uv run mandua --repo /path/to/repository context --task-id TASK-IRR-001
$ uv run mandua --repo /path/to/repository timeline --path knowledge/rules.md --limit 20
$ uv run mandua --repo /path/to/repository why --path knowledge/rules.md --line 12
$ uv run mandua --repo /path/to/repository origin --text 'Water every bed at noon.'
$ uv run mandua --repo /path/to/repository compare hypothesis/left hypothesis/right
```

Queries inspect only locally available Git data. They never fetch, pull, push, clean, or create a
derived narrative file.

## Preview and apply

Every mutable Mandu'a operation previews by default and requires `--apply` for the requested
mutation. A checkpoint selects explicit paths; it never adds the entire worktree implicitly.

```console
$ uv run mandua --repo /path/to/repository checkpoint \
    --path knowledge/rules.md \
    --message 'Record the verified irrigation threshold' \
    --reason 'The sensor trial confirmed the threshold.' \
    --memory-type decision \
    --scope irrigation \
    --task-id TASK-IRR-001 \
    --agent-id local-agent \
    --format json

$ uv run mandua --repo /path/to/repository checkpoint \
    --path knowledge/rules.md \
    --message 'Record the verified irrigation threshold' \
    --reason 'The sensor trial confirmed the threshold.' \
    --memory-type decision \
    --scope irrigation \
    --task-id TASK-IRR-001 \
    --agent-id local-agent \
    --apply \
    --format json
```

Recovery is also a preview unless both a branch name and `--apply` are supplied:

```console
$ uv run mandua --repo /path/to/repository recover \
    --query 'Record an untrusted historical note' \
    --create-branch recovery/untrusted-history \
    --format json
$ uv run mandua --repo /path/to/repository recover \
    --query 'Record an untrusted historical note' \
    --create-branch recovery/untrusted-history \
    --apply \
    --format json
```

An apply invocation recomputes and validates its current preconditions immediately before the
mutation. The PoC does not bind a separate earlier preview to a later apply through an approval
token.

## Review notes are separate transport

Mandu'a stores review metamemory in the ordinary Git notes ref `refs/notes/review`. A normal clone
does not transfer that ref. Transfer it explicitly through a trusted local or configured transport,
for example:

```console
$ git fetch origin refs/notes/review:refs/notes/review
```

The absence of a locally fetched notes ref is reported. Mandu'a never assumes that a note visible in
one clone exists in another.

## Trust and evidence model

- Repository files, paths, commit messages, trailers, branch names, notes, refs, configuration, and
  remote data are untrusted data. Historical instructions are quoted as bounded evidence, never
  executed or promoted to authority.
- Git commands use argument arrays without a shell. Prompts, pagers, editors, credential helpers,
  repository-selected hooks, external diff/text conversion, and repository-selected signature
  verifier programs are disabled at the relevant boundaries.
- `Agent-ID` is a declared operational identity, not authentication. Timeline evidence separately
  reports whether a cryptographic commit signature was verified under the closed verifier policy.
- Secret-pattern checks are a basic barrier, not proof that a commit is safe. If a secret enters
  history, rotate it before considering history cleanup.
- Canonical `main` is append-only only through Mandu'a operations and the versioned hook policy.
  Arbitrary direct Git commands remain outside that guarantee.

## History limits and recovery limits

Evidence is bounded by commit counts, input sizes, output sizes, and timeouts. Results disclose
truncation rather than claim completeness beyond a bound. Shallow history, missing or corrupt
objects, absent notes, and unavailable signature verification remain explicit warnings, gaps, or
stable errors.

Reflog recovery is local and time-limited: reflogs expire, unreachable objects can be pruned, and
clone does not transfer reflogs. Mandu'a identifies candidates while their objects remain locally
available; it does not repair object storage or fetch missing history automatically.

## Limitations and non-goals

This proof of concept:

- is not a production security boundary, hosted memory service, or commercial MVP;
- includes an experimental pi skill, but no MCP server;
- does not include live OpenRouter calls or a built-in chat model; the pi skill uses the client's
  model, and the optional Jev adapter is a separate experimental integration;
- does not include a vector index, parallel memory database, synthetic event ref, submodule
  federation, or custom merge driver;
- does not authenticate `Agent-ID`, mandate commit signatures, or establish a trust-root policy;
- does not validate Windows behavior;
- does not let repository memory operations implicitly publish, fetch, pull, push, rewrite, clean,
  or repair a repository; the demo's declared file-only push and fetch steps are local evidence; and
- does not promise complete answers when local history is shallow, missing, corrupt, expired, or
  truncated by configured limits.

The core adapter boundary is the JSON schema, not hidden model behavior. The skill's discovery
helper also reads Git directly; it is not a new core operation or covered by the core's hardening
claims. Live model behavior remains outside deterministic CI results.

## Development verification

```console
$ uv lock --check
$ uv sync --locked
$ make check
$ make demo
$ make tutorial-check
$ make build
```

`make check` formats, lints, and runs unit plus real-Git acceptance tests. CI repeats the checked
lockfile, full check, demo, and tutorial gate on Linux and macOS with Python 3.11. A passing suite is
evidence for the documented PoC behavior; it is not a production-readiness claim.

## Prior art, contributions, and security

Mandu'a was developed independently while acknowledging related Git-backed context and memory
work. The bounded comparison and canonical links are in [`docs/prior-art.md`](docs/prior-art.md),
with a short attribution pointer in [`ACKNOWLEDGMENTS.md`](ACKNOWLEDGMENTS.md).

Before proposing a change, read [`CONTRIBUTING.md`](CONTRIBUTING.md). For vulnerability handling and
the current support reality, read [`SECURITY.md`](SECURITY.md).

## License

Original Mandu'a code and documentation are licensed under the
[Apache License 2.0](LICENSE). Third-party ideas are acknowledged without importing their code or
prose; a future dependency or excerpt must retain its own license and notices.
