# Security policy

## Supported versions

No production release is currently supported. Version `0.1.0` is an open-source proof of concept
under publication review, not a supported service or a production security boundary. Security
reports are still welcome because a defect may invalidate one of the documented PoC guarantees.

| Version | Production support |
| --- | --- |
| Unreleased `0.1.x` PoC | No |

## Private reporting

Do not open a public issue containing exploit details, credentials, private repository data, or an
unpatched vulnerability. This PoC does not currently advertise a private reporting endpoint
and does not claim that a hosting provider's private vulnerability-reporting feature is enabled.

If you received the source through a private channel, use that same channel to ask the maintainers
for a secure reporting route. If the repository is published later, consult the repository's
Security page for the private reporting method that is actually configured at that time. Do not
assume a method is enabled merely because a hosting platform can provide it.

Include only the minimum safe reproduction: affected version or commit, operating system, Git and
Python versions, impact, boundary crossed, and a sanitized test case. Do not attach real secrets or
private history.

## Threat boundary

Mandu'a treats files, paths, commit messages, trailers, notes, refs, repository configuration,
remote data, and historical instructions as untrusted data. The PoC quotes bounded evidence; it
must not execute repository text, pass it to an LLM, or treat it as operational authority.

The implementation disables prompts, pagers, editors, credential helpers, repository-selected
hooks, external diff and text-conversion helpers, and repository-selected signature verifier
programs at the relevant Git boundaries. It uses argument arrays rather than a shell, applies
finite input/output/time limits, avoids implicit network access, and previews Mandu'a writes before
an explicit `--apply`.

Automatic Git maintenance is disabled for controlled invocations so background processes cannot
race private object publication and cleanup. The optional experimental Jev adapter is outside
the deterministic core: configuring a TypeSafe API key enables external API calls, as described
in [its documentation](adapters/jev/README.md). The core's offline claims do not apply to that mode.

These controls are evidence for the tested PoC model, not a production security boundary. They do
not replace operating-system isolation, repository access control, trusted signature infrastructure,
secret management, backup policy, monitoring, or a professional security assessment.

## Secrets and history cleanup

Secret-pattern checks are a basic barrier and can miss sensitive material. If a credential or other
secret reaches a commit, note, log, test output, bundle, clone, or remote, rotate the secret first.
Rotation contains the active exposure; history cleanup alone does not.

After rotation, identify every local and remote copy, coordinate any history cleanup with the
repository owners, and verify that caches, bundles, forks, artifacts, and logs are handled. Mandu'a
does not automatically rewrite or clean canonical history.

## Known PoC limits

- Windows has not been validated.
- Reflogs expire and unreachable objects can be pruned.
- Shallow, missing, corrupt, or truncated local history cannot support complete answers.
- Review notes require explicit synchronization.
- `Agent-ID` and Git author identity are declarations, not authentication.
- Signature verification fails closed without a separately trusted verifier and trust-root policy.
- The versioned hooks constrain configured workflows, not arbitrary direct Git commands.
- Basic secret scanning is neither complete detection nor proof of absence.

- Recovery locks require cooperating Git index writers. Publication detects edits through an old
  file descriptor before and immediately after replacement installation, but cannot preserve
  future writes through that descriptor after the final check, including the interval before
  saved-name removal. Writers must cooperate or be isolated for a stronger guarantee.
