# Register the Mandu'a demo skill

These instructions are for the coding assistant receiving the registration
request. Register the existing official skill; do not invent a replacement.
Registration does not authorize creating the demo or querying a user's memory.

## Keep the implementation together

Obtain the complete source repository from https://github.com/fermellone/mandua
in a persistent local directory, or reuse an existing checkout of that repository.
Do not overwrite local changes or replace an existing installation silently.
The canonical skill is skills/mandua/SKILL.md inside that checkout. Read it.

The helpers depend on their position inside the full checkout. Copying just the
skill folder to a client's skills directory breaks that relationship. Keep the
original skill and scripts in place.

Verify that Git, uv and Python 3.11 or newer are available. Prepare the project's
locked Python environment using its non-editable installation mode, as described
in docs/pi.md. Use uv, not pip. Resolve missing prerequisites within the user's
permissions; report any setup that requires their intervention.

## Register with the current client

Use the current client's documented local skill mechanism. Consult its installed
help or official documentation if necessary; do not guess a configuration path.
Use one registration method to avoid duplicate skills.

For pi, register the complete checkout as a local package, using its package.json.
The repository's docs/pi.md contains the maintainer installation details.

For another client, prefer a supported registration of the original skill
location. If the client requires a skill file in its own directory, create a
small local entry named mandua with valid skill metadata. Its instructions must
load the canonical SKILL.md at the checkout's absolute path when invoked, resolve
SKILL_DIR to that original directory, and follow those instructions. Do not copy
or relocate the helper scripts. Keep this entry limited to the Mandu'a demo;
do not change unrelated global instructions or credentials.

Check that the client can discover the entry, the canonical document exists, and
the helper's help command runs from outside the source checkout. These are setup
checks, not permission to start another model conversation. Do not invoke pi,
Claude or Codex to send a test prompt automatically.

Report the installed location, any required restart, and any check you could not
complete. Distinguish registration checks from a successful conversational test.
Stop here. The user will separately ask to prepare the demo.

## Current validation boundary

The pi package and conversational demo have been exercised locally. The generic
registration instructions are intended for other local coding clients, including
Claude Code and Codex, but have not yet been verified end to end in those clients.
Do not describe them as tested integrations until that verification is performed.
