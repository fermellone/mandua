# Local agent validation

## Scope and status

This report records user-run Codex trials reviewed on 2026-09-21. They exercised
the local candidate based on `c318f8f`. Changes to memory selection, offline
launchers and the agent view were uncommitted during those trials. The trials are
not evidence that these changes were published or that remote CI passed for this
candidate.

The user supplied each prompt. Task logs were inspected for the selected memory,
commands, returned evidence and final answer. These are a few controlled examples,
not a statistically representative benchmark. Complete transcripts, private task
identifiers and machine-specific paths are not included here. Model settings and
token usage were not captured consistently, so no model-normalized performance or
cost comparison is claimed.

The earlier pi garden trials exercised previous skill revisions. The current
general selection flow and agent view were exercised in Codex; they have not been
retested conversationally in pi. Claude Code, Claude Cowork and ChatGPT Work have
not been verified end to end for this candidate.

## Fictional library cases

Both cases use a memory repository separate from the source checkout and the
client's working directory. The prompt names Mandu'a and provides the memory's
absolute path. Registration alone is not tested as implicit source selection.

The shared history contains:

1. An undecided loan-period policy in `knowledge/loans.txt`.
2. A 14-day proposal: shorter loans keep popular books in circulation.
3. A competing 28-day proposal: longer loans allow more reading time.
4. Integration of the 14-day proposal with `Decision-ID: DEC-BIB-001` and the
   recorded reason that the library prioritizes circulation.
5. A review note confirming the 14-day policy.

### Case A: reasons without outcome measurements

This repository contains the policy and its history, with no measurement report.
The first prompt asks which alternatives were considered and why one was chosen.
A follow-up asks whether the policy improved circulation and which measurements
support that claim, without repeating the path.

### Case B: measurements in separate files

A separate copy preserves that history and adds `reports/circulation.csv` and
`reports/collection-notes.md` in a later commit. A subsequent commit adds links to
both reports in the policy file. The discovery output exposes these links but not
the CSV rows: answering the measurement question requires an additional read.

The synthetic CSV contains:

```csv
period,start_date,end_date,open_days,loans,unique_borrowers,available_books
before,2019-12-01,2019-12-28,20,120,60,200
after,2020-01-02,2020-01-29,20,150,75,200
```

The collection notes define loans as completed checkouts excluding renewals and
describe two 28-day windows, one library, adoption on 2020-01-01, and a membership
campaign throughout the second window. There was no random assignment, comparison
library or parallel control group. Wait times and overdue returns were not measured.
All records are fictional, not facts about the operator or a real institution.

The prompt asks, in one new conversation:

> Use Mandu'a to consult this fictional library memory: <absolute memory path>.
> Which alternatives did they evaluate and why did they choose one? Is it
> demonstrated that choosing 14 days improved book circulation? What concrete
> measurements support it? Separate recorded reasons from your inferences and
> cite the evidence.

The actual trials used Spanish equivalents. The report paths and expected answer
were not given in the prompt. The policy's links were an intentional discovery
aid; this does not test finding unrelated or unlinked reports in a large repository.

## Observed results

| Trial | Result | Remaining limitation |
| --- | --- | --- |
| General selection, before the agent view | Selected the explicitly named library; recovered 14/28-day alternatives and the recorded selection reason. | Does not demonstrate implicit activation from an ambiguous prompt. |
| Follow-up in the same conversation, before the agent view | Kept the selected library without another path. | Incorrectly treated empty citation/change metadata and a full metadata scan as proof of missing measurements. |
| Case A with the agent view | Used the new view, recovered reasons and inspected the committed policy; did not use internal fields as arguments in the final answer. | Still said there were no measurements more broadly than the inspected evidence justified. It also repeated the decision lookup inside the corrections query. |
| Case B with the agent view | Followed report links, read committed CSV and collection notes, calculated the 25% increase and distinguished the observed increase from a demonstrated causal effect. | One successful case; auxiliary reads and repeated commit-body reads leave room for efficiency improvements. |

In Case B, the answer reported 120 to 150 loans, 60 to 75 unique borrowers and
6 to 7.5 loans per open day, while open days and collection size stayed constant.
It identified the concurrent membership campaign and missing controls. It did not
claim that the shorter period caused the increase. Loans per borrower remained
2 in both windows; the answer did not mention that additional calculation, which
was not required to support its cautious conclusion.

The Case B evidence path was discovery, two path-history queries, then direct Git
reads of the committed reports at a resolved OID. This is the skill plus local
Git tools, not proof that one core Mandu'a operation performs semantic research.
The client also read its own project memory and the TypeSafe skill; library claims
were supported by the selected repository, and no Jev API call was observed.

Recorded timings, rounded for readability:

| Trial | Complete turn | Mandu'a query time |
| --- | ---: | ---: |
| Initial explicit library selection | 35.8 s | 0.40 s, one query |
| Follow-up with the earlier machine output | 21.3 s | 0.31 s, one query |
| Case A, combined question with the agent view | 111.5 s | 0.88 s, three queries |
| Case B, combined question with the agent view | 82.9 s | 0.90 s, three queries |

Turn times include client work beyond repository queries. Different prompts,
conversation state and unrecorded model settings prevent attributing those timing
differences to the output format. No token savings or latency improvement is established.

## Automated coverage and verification record

- [Agent-output acceptance tests](../tests/acceptance/test_agent_output.py) put
  measurements in another file and verify that a decision lookup still declares
  file contents unsearched and repository-wide absence unestablished. They also
  cover empty lookups, error exits and the unchanged machine JSON contract.
- [Renderer tests](../tests/unit/test_renderers.py) check citations, chronology,
  revision selection, incomplete-history limitations, inference separation and
  write preview/application state without mutating the original result.
- [Portable helper tests](../tests/acceptance/test_pi_skill.py) exercise real
  installed launchers from another directory, with spaces in paths, an unusable
  cache and a changed build requirement. Queries must not rebuild the environment.
- [Correction tests](../tests/acceptance/test_pi_corrections.py) cover verified
  links, unmerged corrections, current committed content and retrieval limits.

During implementation, the full suite recorded **1,151 passed and one failed**.
The failure was `test_console_script_and_module_help_are_identical_and_list_every_command`:
dependency synchronization emitted installation messages during a help comparison.
That test now compares prepared entrypoints without synchronization. The final
affected set subsequently recorded **25 passed**, including that corrected test
and a chronology-preservation test added after the full run began. This is not
presented as a fresh all-green full-suite run on the final candidate. Formatting,
lint and whitespace checks also passed at that implementation checkpoint.

At documentation closure, a fresh run covering project metadata and packaging,
agent output, portable helpers, corrections, renderers and the corrected help
comparison recorded **43 passed**. Formatting, lint and whitespace checks passed
again. This focused verification does not replace the full-suite qualification above.

Before publication, a fresh complete `make check` run recorded **1,153 passed**
in 837.20 seconds on the final code candidate, with formatting and lint also
passing. The credential-free demo succeeded, and `make tutorial-check` recorded
**7 passed**. These local results supersede the earlier incomplete full-suite
qualification; verification of the pushed commit in remote CI is a separate check.

These automated checks validate contracts and execution, not model accuracy.

## Open validation work

- Keep absence claims scoped to inspected records; the Case A wording is still
  an observed limitation, not a closed reliability issue.
- Test source switching, ambiguous requests and larger or unlinked evidence sets.
- Repeat the current candidate in other clients and with recorded model settings.
- Measure tokens and redundant retrieval before making efficiency claims.
- Initial ingestion, continuous ingestion and write authorization are outside
  this read-only skill trial. Client-memory coexistence was observed in these
  traces, not proven as isolation or resistance to contamination.

This round supports an experimental, explicitly selected, read-only memory
workflow. These conversational trials do not replace publication approval or
verification of the eventual committed candidate.
