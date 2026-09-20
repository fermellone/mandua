# Release hardening evidence — 2026-09-06

This is a local release-preparation evidence record, not an independent audit certification. The delivery sequence
is reproduction, checkpoint reconciliation, captured Git execution authority, complete release
checks and review, commit/integration into main, and a clean-install release candidate.
Actual public publication requires separate operator approval.

## Reproduced defects and corrections

The checkpoint regression exercises real ref updates, anomalous success/failure results, and a
concurrent third-party index. Inline ref-only rollback was removed: the emergency combined
ref/index classifier now decides whether rollback is safe. A rejected ref update cannot become
proven-old before its index snapshot is verified. The focused checkpoint suite passed 80 tests
before the execution isolation changes; the final full gate must cover the combined version.

Launch-boundary regressions replace a captured root immediately at Popen. The original runner
wrote objects, refs/reflogs, or an index into replacement roots in five cases. An attempted macOS
volume-path projection broke ordinary Git writes and was discarded.

Git mutating commands now execute in a private directory entered using an inherited directory
descriptor. Objects, refs, index, and the integration worktree are private; the source object
store is only a read-only alternate. Publication checks object identity and uses held source
directory descriptors. The regression matrix covers five roots across object, ref, index,
commit, and merge operations, plus temporary-index parent replacement with and without an
explicit repository authority. Ordinary successful writes are tested separately.

## Additional regressions found during broad verification

- A copied index had stale stat data during merge abort. A separately budgeted private
  update-index refresh now refreshes metadata without staging worktree changes.
- Native write-tree can rewrite an index even when bytes are unchanged. Publication retains
  that observable replacement instead of silently skipping it.
- Concurrent notes writers encountered an owned lock as an ambiguous process exception.
  Bounded lock acquisition and an explicit pre-publication rejection preserve retry semantics.
- A file or nested worktree directory changed between validation and publication could lose
  concurrent data. Held parent descriptors, detached-entry validation, and exclusive file
  installation preserve concurrent entries and reject the publication.
- A FIFO could block before file-type validation. Reads now open nonblocking and reject
  nonregular entries. The regression unblocks a failed reader rather than leaking a thread.
- Cleanup could replace KeyboardInterrupt and leave later locks unvisited. All lock cleanup
  attempts now run and attach failures to the original interruption.
- Administrative file permissions must be captured, not invented: otherwise a normal commit
  incorrectly enters recovery after its ref has already been published.

## Earlier candidate verification (8d34a04)

That candidate passed formatting, lint, and whitespace checks. All **1,114 tests passed** on
macOS / Python 3.13.7, split into three independent groups:

| Group | Passed | Wall time |
| --- | ---: | ---: |
| Integration | 154 | 382.19 seconds |
| Demo and distribution | 97 | 240.35 seconds |
| All remaining unit and acceptance tests | 863 | 441.01 seconds |

The JUnit reports were compared against fresh pytest collection: every collected test executed
exactly once, with no omissions, duplicates, failures, errors, or skips. This covers the complete
test suite used by `make check`; the final run was partitioned to shorten wall time.

The source demo completed. The generated tutorial check passed 7 tests; the focused conformance
and security-boundary check passed 70 tests. Wheel installation outside the checkout and its
demo succeeded on Python 3.13.7 and Python 3.11.13. Both installed demos reported eight verified
claims, a verified bundle, a complete operation log, and no network access.

The initial complete diagnostic run had 15 failures and 7 fixture errors. These exposed a
correction-result signature mismatch, demo audit incompatibilities, and report-size exhaustion.
Corrections now propagate checkpoint recovery warnings. Demo audit records retain the exact
Python launcher and Git command; report validation recognizes only the fixed worker structure,
and redaction does not mistake Python's -S option for Git search data. The finite report limit
is now 2 MiB to accommodate the recorded launcher evidence and longer installation paths.

A late replacement of the source log root was reproduced and corrected by publishing through
the captured log descriptor. A successful file detach followed by an interruption was also
reproduced: cleanup classifies the detached entry before deciding whether it is disposable.
The final test groups include these corrections.

## Independent review follow-up

A new independent read-only review completed after an earlier service-interrupted attempt.
It reproduced three additional findings against candidate `8d34a04`:

- Recovery released its original index lock before classifying and rolling back the ref.
  Emergency reconciliation now reacquires the index lock and holds it through classification
  and any compensating ref update. A real concurrent `git add` is rejected by that lock.
- A writer holding the original file descriptor could edit the detached inode between its
  validation and installation of the replacement. Publication checks the saved original again
  after installation. Detected edits reject publication and remain reachable by a recovery name.
- A directory-close failure could mask an interruption and skip private-directory cleanup.
  Outer cleanup now attempts every owned resource and attaches failures to the original exception.

Each finding has a regression that failed before its correction. Independent re-review of the
three corrections completed with no unresolved important findings; the reviewer independently
ran all three new regressions and the three existing isolated-execution unit tests (6 passed).
This is approval of the reviewed corrections, not a production security certification.

### Concurrency scope

The recovery lock excludes writers that follow Git's index-lock protocol. Worktree publication
checks the detached original before and after replacement installation and preserves detected
concurrent data. It cannot guarantee preservation of future writes through an old file descriptor
after the final check, including the interval before saved-name removal. POSIX pathname locks
do not revoke existing
file descriptors. Do not write concurrently through retained descriptors during publication;
stronger guarantees require all writers to cooperate or operating-system isolation.

## Review and delivery scope

The reviewed source passed all **1,124 tests** on macOS / Python 3.13.7:

| Final group | Passed | Wall time |
| --- | ---: | ---: |
| Integration | 154 | 383.69 seconds |
| Demo and distribution | 97 | 226.42 seconds |
| Remaining unit and acceptance tests | 873 | 468.01 seconds |

JUnit records match fresh collection exactly: every case appears once in the final successful
groups, with no failures, errors, or skips. Formatting, lint, whitespace, and generated tutorial
checks passed. The source demo verified eight claims, its bundle, and a complete operation log
with no network access. Package tests verified clean installation outside the checkout.

The initial distribution attempt could not refresh cached build dependencies because network
access was unavailable. Repeating the group with UV_OFFLINE=1 inherited by child processes
passed all 97 tests using cached dependencies; no source change was needed.

Two older fault injectors also repeated their failures inside the new recovery lock. Tests now
cover single and repeated failures separately: a single failure preserves the original exception;
repeated recovery failure reports uncertainty with that exception as its cause. Ref/index and
cleanup assertions remain, and independent review approved the expanded expectations.

Final distribution hashes, installed-demo evidence, and the integrated revision belong in the
accompanying release manifest, generated after integration. Local integration and artifact preparation do not create a public tag, push,
registry upload, or public release. Linux CI was not executed on this local macOS host.

## Linux ARM64 validation in Docker

The same runtime candidate was subsequently validated on Linux ARM64 in Docker Desktop,
using Debian Bookworm, Python 3.11.16, Git 2.50.1, and uv 0.8.22. The source was copied into
the container filesystem, without host-directory mounts. Checks ran as an unprivileged user
with networking disabled and Linux capabilities dropped.

All **1,124 tests passed in 121.30 seconds**, with no failures, errors, or skips. JUnit records
match fresh collection exactly. Formatting, lint, generated tutorial, distribution build,
source demo, and the existing release wheel installed outside the checkout also passed.
Both demos verified eight claims, their bundle, and complete operation logs without network use.

The initial environment used Git 2.39.5 and reported 1,116 passes, seven failures, and one skip.
Its archive copy also lacked a populated Git index needed by the hook-mode test. After using
the documented development baseline Git 2.50.1 and populating the copied index, only one test
defect remained: a forged-worktree fixture used `head` instead of Git's actual `HEAD` filename.
Correcting that capitalization preserves the security assertion on case-sensitive filesystems.
The seven related fixture cases also passed on macOS. No runtime code changed.

This ARM64 run validates Git 2.50.1 in Docker; it does not establish older Git compatibility.
Git 2.39.5 did not pass the full validation. The configured external Linux CI job was not run.
The release manifest accompanies the retained environment, test, build, and demo evidence.

## Linux x86_64 validation under emulation

The same source and release wheel were subsequently checked in Docker `linux/amd64` on the
ARM64 macOS host. Both the image architecture and the running process were verified as
amd64/x86_64. The environment used Debian Bookworm, Python 3.11.16, Git 2.50.1, and uv 0.8.22,
with the same unprivileged, network-disabled, no-host-mount setup as the ARM64 run.

All **1,124 tests passed in 1,797.57 seconds**, with no failures, errors, or skips. JUnit
records match fresh collection exactly, and the tested source/test hashes match the local
candidate. Formatting, lint, generated tutorial, package build, source demo, and the existing
release wheel installed outside the checkout passed. Both demos verified eight claims,
their bundles, and complete operation logs without network access. No code or test changes
were required for x86_64.

This is compatibility evidence under emulation, not a native x86_64 performance measurement.
At that stage, native x86_64 execution and external CI remained unverified. Evidence and image identity are
retained with the release manifest under `linux-amd64`.

## External CI and Git 2.55 maintenance race — 2026-09-20

Commit `75c7e06` passed **1,131 tests**, the source demo, and **seven tutorial tests** on
GitHub-hosted Ubuntu x86_64 and macOS ARM64 with Python 3.11 and Git 2.55.0.
[CI evidence](https://github.com/fermellone/mandua/actions/runs/35500833794).
This establishes native Linux x86_64 compatibility in addition to the earlier emulated run.

The macOS job exposed intermittent private-object namespace failures during integration.
Git's automatic maintenance could outlive the commit process and leave `objects/maintenance.lock`
present at publication. A test-only scheduling delay in a temporary Git 2.55 build reproduced
the failure with that exact filename; both affected integration cases passed with the fix.
The controlled Git invocation now sets `maintenance.auto=false` and `gc.auto=0`.
A real-Git trace regression checks that isolated commits launch no automatic maintenance child.
Unknown object namespaces remain rejected; the initial speculative temporary-directory exception
was removed. This change does not expand the previously documented concurrency guarantees.
