# Talk to the demo memory

Mandu'a is a proof of concept. This walkthrough lets you talk to the memory of a
fictional community garden using your existing coding assistant. It is a demo,
not a production memory service.

Use a local client that can read files and run tools. The conversational workflow
has been tried in pi. Registration in Claude Code and Codex has not yet been
verified end to end. A browser-only chat without local tools cannot run this demo.

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
