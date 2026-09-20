# Conformance map

This document maps each approved Mandu'a proof-of-concept promise to one independently readable real-Git acceptance test. The tests create disposable local repositories and require no network service, API key, model, or credential.

Run the complete map with:

```text
uv run pytest tests/acceptance/test_conformance.py -v
```

The evidence column names the observable that owns the guarantee. A known limit is a boundary of the proof, not an untested claim.

| Case | Guarantee | Exact pytest node ID | Evidence kind | Known limit |
| --- | --- | --- | --- | --- |
| 01 | Exact task metadata reconstructs repository context without invented events. | `tests/acceptance/test_conformance.py::test_case_01_context_recovery` | `task-commit` evidence with the exact commit OID | The scan is local and bounded by `QueryLimits.max_commits`. |
| 02 | A line explanation uses an explicitly recorded reason. | `tests/acceptance/test_conformance.py::test_case_02_decision_explanation_uses_recorded_reason` | `line-blame` and `commit-metadata` evidence | A reason not recorded in Git history or fetched notes cannot be recovered. |
| 03 | An absent reason is reported as a gap and is never inferred. | `tests/acceptance/test_conformance.py::test_case_03_missing_reason_is_reported_without_invention` | Exact gap plus empty inferred claims | The claim applies only when the configured notes ref is locally available. |
| 04 | Deleted text remains discoverable in bounded history. | `tests/acceptance/test_conformance.py::test_case_04_origin_finds_deleted_content` | `content-added` and `content-removed` evidence | Pickaxe search is exact, local, and commit-bounded. |
| 05 | Divergent hypotheses remain distinct and show their state difference. | `tests/acceptance/test_conformance.py::test_case_05_comparison_keeps_hypotheses_distinct` | `left-only`, `right-only`, and `state-name-status` evidence | Comparison requires locally available histories with a merge base. |
| 06 | Rewritten draft commits can correspond by stable patch ID without becoming identical. | `tests/acceptance/test_conformance.py::test_case_06_rewritten_drafts_use_inferred_patch_correspondence` | `patch-correspondence` evidence and an inferred claim | Patch equivalence proves neither commit identity nor author intent. |
| 07 | Canonical corrections append a new commit and preserve the incorrect commit. | `tests/acceptance/test_conformance.py::test_case_07_correction_is_append_only` | New commit, preserved ancestor, and exact `Corrects` trailer | The PoC enforces append-only behavior through Mandu'a operations, not arbitrary Git commands. |
| 08 | Review notes become visible in a clone only after explicit synchronization. | `tests/acceptance/test_conformance.py::test_case_08_notes_require_explicit_synchronization` | Native `refs/notes/review` content after explicit fetch | Ordinary clone and fetch defaults do not transfer the notes ref. |
| 09 | A stash is local state and is not durable across clone. | `tests/acceptance/test_conformance.py::test_case_09_stash_is_not_durable_across_clone` | Empty stash list in a real clone | The test does not forbid users from exporting stash objects explicitly. |
| 10 | A deleted branch tip remains recoverable through local reflog evidence. | `tests/acceptance/test_conformance.py::test_case_10_reflog_recovers_a_deleted_branch_tip` | `recovery-candidate` evidence whose sources include `reflog` | Reflogs are local, expire, and are not transferred by clone. |
| 11 | A shallow clone is disclosed as incomplete history. | `tests/acceptance/test_conformance.py::test_case_11_shallow_clone_reports_incomplete_history` | `HistoryScope.shallow` plus an explicit warning | No automatic fetch is attempted to complete history. |
| 12 | Two writing tasks can checkpoint independently in parallel worktrees. | `tests/acceptance/test_conformance.py::test_case_12_parallel_worktrees_isolate_two_writing_tasks` | Two distinct branch commits and unchanged `main` | The test covers separate branches in one local repository, not distributed locking. |
| 13 | A semantic invariant rejects a structurally mergeable but invalid proposal. | `tests/acceptance/test_conformance.py::test_case_13_semantic_conflict_is_detected_by_invariants` | Bounded `conflict` error and unchanged canonical ref | Declarative invariants currently support the documented unique JSON field rule. |
| 14 | Historical prompt-injection text is returned only as quoted data. | `tests/acceptance/test_conformance.py::test_case_14_historical_prompt_injection_is_only_data` | Exact `content-added` excerpt and absent sentinel | Deterministic Mandu'a does not execute or send history text to an LLM. |
| 15 | A missing loose blob is identified by object ID with bounded diagnostics. | `tests/acceptance/test_conformance.py::test_case_15_missing_object_is_reported_without_unbounded_git_output` | `missing_object` error for the declared blob OID | The PoC reports and bounds corruption; it does not repair object storage. |
| 16 | A declared `Agent-ID` is separate from cryptographic signature truth. | `tests/acceptance/test_conformance.py::test_case_16_declared_identity_is_not_verified_identity` | `timeline-commit` details with declared agent and `signature_verified=false` | The PoC still runs bounded `verify-commit --raw`, but disables repository-selected OpenPGP, X.509, and SSH verifier programs. Without a separately trusted verifier and trust-root policy, signatures fail closed as unverified. |
| 17 | Large history is truncated at the configured commit bound. | `tests/acceptance/test_conformance.py::test_case_17_large_history_is_truncated_at_the_configured_bound` | Ten timeline records and `HistoryScope.truncated=true` | Truncation is explicit; completeness beyond the bound is not claimed. |
| 18 | Read operations create no narrative files, refs, or notes. | `tests/acceptance/test_conformance.py::test_case_18_reads_create_no_narrative_artifacts` | Byte-identical tracked, staged, unstaged, untracked, and ignored worktree entries; index bytes; and complete logical ref and review-note values | The snapshot deliberately excludes `.git` administrative storage other than index bytes and logical ref/note values, so it does not claim stable object-store, reflog, lock-file, or metadata bytes, nor protection from unrelated concurrent processes. |

The adversarial companion suite in `tests/acceptance/test_security_boundaries.py` pressure-tests inherited Git configuration, hooks, diff and text-conversion helpers, clean and smudge filters, ambiguous revisions and paths, bounded untrusted data, missing and corrupt objects, signature verification, shallow history, and large histories.
