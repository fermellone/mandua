# Talk to the demo memory

Mandu'a is a proof of concept. This walkthrough lets you talk to the memory of a
fictional community garden using your existing coding assistant. It is a demo,
not a production memory service.

Use a local client that can read files and run tools. The conversational workflow
has been tried in pi. Registration in Claude Code and Codex has not yet been
verified end to end. A browser-only chat without local tools cannot run this demo.

## Before starting: choose a clean environment

We recommend trying this PoC in a clean client environment, especially if your
usual assistant already has a memory system. Previous conversations, saved
memories, global instructions, other skills and connected retrieval tools can
influence its answers. For this trial, the garden records should be the only
source of information about the case.

A new conversation and an empty folder are useful, but they do not necessarily
disable client-wide memory or configuration. Choose an option that fits your
client and experience:

| Option | What to do | What to check |
| --- | --- | --- |
| Separate client profile | If your client supports isolated profiles or configuration directories, create a fresh one just for this trial. Register only the Mandu'a skill there. | Confirm it does not inherit your usual memory, instructions, plugins or connections. |
| Separate operating-system user | Use a dedicated local user account and set up your coding client there. | Avoid importing your usual client settings; check whether signing in restores account-level memory. |
| Docker container | Run a compatible command-line client and Mandu'a together in a disposable Linux container with fresh client configuration and a dedicated demo directory. | Do not mount your usual home directory, client configuration or memory folders. Running only Mandu'a in Docker leaves the host client's context unchanged. |
| Virtual machine | Set up a fresh client and the demo inside a separate virtual machine. | Avoid restoring or sharing the host client's configuration and memory folders. |

Start with a separate profile if your client supports it; a separate user account
is another option. Containers and virtual machines require more setup. They are
environment-isolation options, not preconfigured Mandu'a installers: this PoC
does not ship a ready-to-use container image for an authenticated coding client.
The conversational demo in a container or virtual machine has not been validated
end to end. Your chosen environment still needs the client, Git, uv and Python,
and the client may require its own sign-in.

Filesystem isolation does not remove memories stored by an online account.
Where applicable, disable that feature for the trial or choose a client mode
that does not use it. Keep your existing memory intact; use a separate environment
rather than deleting your normal setup. If isolation is incomplete, note that
when interpreting or sharing the results.

Complete the following steps inside the environment you chose.

## 1. Register the skill

Copy this message into your client:

> Register the official Mandu'a skill for this client, following
> https://github.com/fermellone/mandua/blob/main/docs/skill-registration.md.
> Only register the skill and prepare what it needs to run. Do not create the demo
> or query any memory yet. Tell me when registration is complete and whether I
> need to restart the client.

Follow any setup instructions your client gives you. Keep the installation in
place so the skill can find its files. You use the model already configured in
that client; Mandu'a does not require another model API key.

## 2. Prepare the demo

Create a new folder for this trial and open it in your client. Restart the client
first if registration required it. Then send:

> Use the Mandu'a skill to prepare its community garden demo in this folder.
> If the demo already exists, reuse it. Tell me when it is ready, without answering
> questions about its history yet.

Wait for confirmation that the demo is ready before continuing. Preparation is a
required step in this walkthrough.

## 3. Read the story

Read [the community garden's story](demo-story.md) to understand what these
fictional memories are about. It gives you the setting, not a list of questions
or an answer key.

## 4. Have a conversation

Ask whatever you want to understand about the garden's memory, in your preferred
language. If your client does not select the skill, ask it to use the Mandu'a skill.
It should ground its answers in the records and say when information is missing.

The records are fictional. Your client may send retrieved evidence to its model
provider, and its normal usage charges or subscription limits apply. The demo
checks how an assistant uses recorded evidence; it does not guarantee that every
answer will be accurate.
