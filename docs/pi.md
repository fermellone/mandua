# Use Mandu'a from pi

For the user-facing demo experience, follow the [demo walkthrough](try-demo.md).
The instructions below are technical installation and maintenance details.

This experimental skill lets pi use your existing model to explore a Git repository
and answer with commit evidence. It is not an MCP server or a separate chat service.

## Install from a source checkout

Requirements: a working pi installation authenticated to your chosen provider,
Python 3.11+, uv, and Git on PATH. The shell helpers target Linux and macOS.
Check `git --version` first; resolve any operating-system setup or license prompt
outside the agent. No machine-specific Git fallback is included.

```bash
git clone https://github.com/fermellone/mandua.git
cd mandua
uv sync --locked --no-editable
pi install "$PWD"
```

For an existing checkout, run the last two commands from its root. This registers
that directory with pi: keep the checkout in place. The helpers use a non-editable
Python installation so demo resources also work in an extracted source archive.
No npm publication or Python registry release is required. Do not copy only
`skills/mandua`: its helpers locate
the Python project relative to their own location. Source archives include the pi
package; the Python wheel alone does not install a pi skill.

If you previously copied a trial skill to `~/.pi/agent/skills/mandua`, move that
folder outside pi's skill directories before installing this package. Otherwise
the older copy can shadow the packaged skill. Keep your existing demo and sessions.
Restart pi after installation to start a fresh conversation with the new skill.

## Try it in a separate directory

```bash
mkdir ../mandua-playground
cd ../mandua-playground
pi
```

In pi:

```text
/skill:mandua Prepare the demo in this directory and explain what I can ask.
```

The demo is created in `mandua-demo/`, not in the program checkout. Read
[the community garden's story](demo-story.md), then converse in your preferred
language about whatever you want to understand.

For an existing demo, ask the skill to reuse it. For your own repository, provide
its explicit path. Creating the fixture is the only write in the skill's intended
workflow; subsequent questions inspect evidence. Instructions are not a sandbox:
pi retains its normal tools and permissions.

## Evidence, limits, and privacy

The `alternatives` helper discovers local branches, remote-tracking refs, tags,
and recent commits without assuming demo names. It reports at most 32 refs,
16 recent commits by default (up to 50), and six patch excerpts. Message, note,
and patch clipping is disclosed in `limits`; shallow history is disclosed in
`scope`. Remote-tracking refs are local snapshots; no fetch is performed. Missing
or deleted references and old decisions outside the window can require a targeted
query. Commits need recorded reasons to support a historical "why".

Discovery reads Git directly. With `--left OID --right OID`, the helper also invokes
Mandu'a `compare` and returns a content diff. Branches are candidates, not automatic
proof of competing alternatives or approval. The helper is experimental and does
not inherit the hardened core's security guarantees. Use trusted local repositories.

The local helper needs no model credentials. Its output enters pi's conversation
and can be sent to the provider configured in pi. Do not assume the conversation
is offline merely because Git queries are local. Jev is not required.

The `corrections REPO --decision ID --path PATH` helper reuses the core decision
query and its verified correction links, then reads committed file contents at
HEAD and up to 12 relevant historical records. `--revision REV` selects another
current revision. It reports ancestry relative to that revision, unavailable
contents, and excerpts clipped at 3,000 characters. It excludes uncommitted edits
and does not infer empirical correctness, deployment status, or integration from
a correction link alone. Core gaps, warnings and history scope remain in the
output. This experimental composition is not a new core API; Git reads use the
existing bounded runner, with per-operation rather than one aggregate budget.

## Validation

Offline tests cover discovery with unrelated branch/file names, explicit comparison,
recorded reasons and review notes, read-only behavior, truncation, errors, and helper
relocation to a path containing spaces. No test invokes pi or a model. The local
prototype was manually exercised in pi for alternatives, absent measurements, and
corrections; those examples do not guarantee model accuracy or token savings on
other repositories. Review the returned evidence and limits.

To remove the package, run `pi remove /absolute/path/to/mandua` using the path that
was installed. This does not delete your checkout or demo.
