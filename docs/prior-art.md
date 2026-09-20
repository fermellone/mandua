# Prior art and related work

**Accessed: 2026-08-31**

Mandu'a does not claim that using Git for agent memory, context recovery, provenance, or explicit
memory storage is new. This document records a bounded engineering comparison to work reviewed
before publication preparation. It describes shared ideas and visible architectural differences; it
does not reproduce third-party source text.

## Git Context Controller

[Git Context Controller](https://github.com/faugustdev/git-context-controller) shares checkpoint,
branch, merge, and context-recovery operations. Its project model maintains dedicated context
artifacts. Mandu'a instead queries a repository's ordinary state and native history and does not
automatically produce cumulative narrative files.

## DiffMem

[DiffMem](https://github.com/Growth-Kinetics/DiffMem) also separates current files from temporal
history stored through Git. Its visible project surface is a memory service for LLM agents with its
own knowledge schema. Mandu'a is a generic deterministic adapter that returns bounded evidence from
an existing repository without requiring an LLM.

## GAM

[GAM](https://github.com/Substr8-Labs/gam) shares the use of Git hashes, provenance, and auditable
history. Its visible project surface stores explicit memories under a dedicated schema. Mandu'a
interprets normal project history as temporal and causal evidence rather than introducing a second
authoritative memory store.

## Separate memory branches

Plugins that store memory in separate Git branches are acknowledged here only as a general design
category, not as a named or cited implementation. That category uses Git objects as an explicit
storage channel; Mandu'a's PoC reads the repository's ordinary DAG, messages, trailers, notes, and
refs and creates no automatic narrative branch.

## The `git-mem` npm package

The [`git-mem` npm package](https://www.npmjs.com/package/git-mem) already occupies the project's
earlier working name and exposes trailers, a CLI, and agent-facing concepts. The canonical package
surface verified for this comparison is the npm package page. No source repository is inferred for
the npm package, and Mandu'a uses the distinct `mandua-memory` distribution name.

## Independent implementation and review boundary

Mandu'a's original code, tests, fixtures, demo, diagrams, and prose are independently written under
Apache-2.0. The projects above are acknowledged for related ideas; their code and wording are not
copied into this repository. A future approved dependency or excerpt must retain its own license and
required notices.

This comparison is not legal or trademark clearance. Names, repository availability, package
registry state, licenses, and trademark status require a separate review before a stable release.
