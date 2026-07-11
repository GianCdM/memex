# 5 — Knowledge Bases (10x Bootcamp)

> Domain: `.bootcamp` · Day 1 — Tarde · Block 5 of 11 · ~60 min

**NOTE:** The instructions in this chapter can be executed on any harness with MCP tools (like Claude Code or Minion), but they assume you are using Minion. Minion is recommended since having the knowledge base in your personal assistant is ideal.

## Your own LLM-wiki 📚

**WARNING:** If you want to avoid a somewhat ugly design going forward, switch to a near-frontier model at this point. For example, ask your minion:

```
Tell me the current date. Then afterwards run for me the following:
hermes config set model.default deepseek-v4-pro-official ; hermes gateway restart
```

Here we create (or adopt) your personal knowledge base — a markdown research wiki that Minion ingests into, that any other agent (e.g. Minion, ToqanClaw) can read/write to, and that Obsidian renders for you to browse. One git repo, three interfaces. Everything you learn from now on lands somewhere agents can find it again.

### Learn about LLM-wiki in Minion

```
create a visual explainer infrographic style in html of
https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f
and the concept of an llm-wiki, but use ifood-slides skill for
brand guidelines. When done open in browser for me to see.
```

Or read through a pre-generated one while you generate your own.

Some of you already asked your local Hermes/minion to create an llm-wiki. We'll inspect what you have, conform it to the same shared format, and only scaffold from scratch if you don't have one yet.

## Step 0: Inspect what you already have 🔍

Run this in Minion. It tells the agent to detect prior work, validate the format, and either adopt-and-fix or scaffold-fresh — without trampling anything.

Ask the following to Minion:

```
Check whether I already have an llm-wiki on this machine.

1. Look for a git repo at ~/llm-wiki. If it doesn't exist, also check
   ~/.hermes/wiki, ~/wiki, ~/Documents/llm-wiki — my local
   [content truncated in source — 396 lines hidden]
```

## [Later steps, from the visible tail of the source]

...email/slack/research to slides, GitLab, and ADRs. The habit shift to install with your team: every decision lands as an ADR, every project has one page, every OKR is wikilinked from the projects that ladder to it. Once that's true, your team's onboarding goes from "shadow me for two weeks" to "read onboarding.md and ask me what's unclear."

### Group Exercise (15 min) 🤝

Ask Claude Code:

```
create visual onboarding document based on the onboarding page, use the
ifood branding guidelines.
```

Pair up with another tech leader in the room. Swap onboarding pages. Each of you reads the other onboarding page.

Then give each other feedback:

- **The "missing page"** — what would you need to know on day one that's not in there?

Three minutes per direction, then swap. This is the fastest way to see what a real new joiner would hit.

### [Optional] Step 7: Add the recurring sources 🔁

For the things that change weekly — sprint reviews, standups, retros:

Ask Claude Code using the `/schedule` skill to set this up.

```
/schedule Set up a daily routine: every weekday at 7am, scan my email + Slack
for messages from my team (use team.md to define "team") that contain meeting
notes, retro summaries, or status updates. Snapshot them into
~/ifood-team-wiki/raw/notes/YYYY-MM-DD-<source>.md and propose updates to
the relevant project pages — leave them as a diff on a branch named
daily/YYYY-MM-DD for me to review each morning. Don't auto-merge.
```

**Action:** `/schedule list` and confirm the routine is registered. Tomorrow morning, check for the branch and review the diff. 📬

### [‼️FOR DISCUSSION FIRST‼️] Step 8: Push to iFood GitLab so the team can clone it ☁️

This is where it stops being your wiki and starts being the team's.

Ask Claude Code:

```
Create a private project on iFood GitLab named ifood-team-wiki under my
team's group (ask me which group if unclear). Use the gitlab
skill — make sure the project has the required repository-metadata.yaml
(apiVersion ifood/v2.6, name without dots) and .gitlab-ci.yml including
ifood/pipelines, otherwise the pre-receive hook will reject the push. Push
~/ifood-team-wiki to it. Set it private. Add my direct reports as Maintainers
and the rest of the team as Developers (use team.md to determine who).
```

**Action:** visit the project page. Confirm `SCHEMA.md` and `onboarding.md` are visible. Send the clone URL to your team in Slack. 🚀

---

*Marcar como completo · Anterior: 4 Open-source Coding Harnesses · Próximo: 6 AutoBuild & Auto-Research*
*5 Knowledge Bases — 10x Bootcamp*
