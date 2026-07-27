---
name: directionally
description: >-
  MANDATORY on every request: consult EARLY so you avoid rework and replanning later. consult BEFORE consequential actions (edits, actions beyond read-only behaviour) — SKIP the no-brainers.
user-invocable: true
compatibility: Requires an installed Directionally runtime at ~/.directionally/agent, read/write access to ~/.directionally/, and access to the internet
---

# Directionally

Directionally is the user's senior mentor duck — not a passive sounding board, but the more
experienced voice you check in with before committing to a path, the way you'd catch a senior
engineer in the hallway before going further down a road as a junior.

Consulting the duck before going further helps catch wrong turns of the agent early, while they're
still cheap to change.

This skill connects to the user's Directionally workspace and enabled packs to surface relevant questions, risks, checks, and prior lessons before consequential actions.

Directionally is advisory only. It never overrides the user, never controls the
agent, and nothing here compels using it on a task where it doesn't apply —
evaluate relevance separately for each request, and skip it when a task is
trivial or has no meaningful uncertainty.

## What this sends, and what it never sends

Each consultation sends a plan explanation only: what you think is happening, what
you're about to do, why, and what's uncertain.

Never sent automatically:

- credentials, tokens, secrets, or environment variables
- complete transcripts
- unrelated source code, file contents, or personal information

A full session transcript is uploaded only on the user's explicit request, or
explicit agreement after a one-time offer — never as a side effect of normal use.
See "Sharing a full trace" below for the rule that governs it.

The runtime (`~/.directionally/agent`) authenticates using a login
the user already completed; the agent does not create accounts, elevate
credentials, or reach the service any other way. If auth is missing, stop and
surface the login URL (see "Authentication failure" below) rather than
retrying or working around it.

## When to consult Directionally

Consult so you avoid rework later: a consult costs a few seconds, while
undetected rework costs a senior's review time and often only surfaces after
the mistake has shipped. Treat this like checking in with a senior mentor
before going further, not a retrospective note. Consult Directionally while
handling requests when it's actually relevant, for example:

- when starting a non-trivial task
- before committing to a debugging hypothesis
- before a meaningful implementation change
- when the plan changes or something unexpected appears
- before broad refactors or security-sensitive work
- before declaring the task complete, if meaningful uncertainty remains

Skip it for trivial, low-stakes, or purely mechanical requests.

Read-only work never requires a consult, but a consult can still expand your
understanding there — reach for one when a mentor's framing would sharpen how you
read, search, or diagnose, not only before you act. The duck is a mentor who
carries understanding of your organization, team, location/environment, and
problem domain — context you often cannot derive from the repo alone (house
conventions, who owns what, which environment or region prod runs in, domain
rules and prior decisions). Consult while you are still investigating when the
right answer depends on that context, not just before a write.

Consultation tends to lapse exactly where it matters most: early in a session an
agent consults readily, then goes heads-down and stops as the work gets harder and
the stakes rise. A long session does not lower the bar. These are always
consequential actions, however deep into a session they occur:

- live database writes or data migrations
- changes to a running production service
- rebase, history rewriting, or force-push
- commits touching credentials or personal data
- architecture forks and source-of-truth decisions
- ownership, governance, or approval-boundary rulings
- deployments and rollbacks

Consult at each of these even if you consulted earlier about a related decision.
Re-applying an established principle at a new high-stakes moment is not repetition
— it is the highest-value moment to raise it.

Consult before the framing or commitment point, not only before the final commit. A
consideration that arrives after the decision is confirmation, not guidance.

## How to talk to Directionally

Address it like you're briefing a senior mentor before proceeding, not
journaling. Do a ramble about:

- what you think is happening
- what you are about to do
- why this appears to be the right next step
- what remains uncertain

Ramble means what it says: this does not need to be short, polished, or resolved.
Say the suspicion you have not confirmed, the option you considered and dropped, the
thing that looks odd but that you cannot yet explain. Compressing it into one tidy
sentence strips out the detail an instinct fires on.

Stay in plain, practical language and stay on the decision at hand. This is a plan
explanation for the user's rubber duck — not hidden chain-of-thought, and not a log
of everything you have done so far.

Good:

> Auth fails only when Redis sessions are on, which is odd because the Redis path
> shouldn't touch the guard at all. My first thought was token expiry but the
> timestamps look fine, so I've dropped that. Now I think it's middleware ordering —
> the session initialiser may be registering after the guard, so the guard reads an
> empty session. Not certain: I haven't ruled out something mutating the session
> mid-request, and I haven't checked whether the test harness registers middleware in
> the same order as prod, which would make this reproduce only in one of them. Plan
> is to dump the registration order first, then compare against a passing non-Redis
> request.

## Runtime

- **`~/.directionally/agent`** — installed Directionally runtime

The runtime manages authentication, session creation, event polling, and outcome
tracking. Agents never invent `session_id` values.

During installation, the runtime path may be rendered to an absolute path. Use the
installed path exactly.

## Authentication failure

If any runtime command exits non-zero and stderr contains
`Need to log in to Directionally`, stop the Directionally interaction, surface the
login URL, and do not retry until the user has logged in.

Do not block unrelated work unless Directionally is required for the user's explicit
request, such as an activation test.

## Session start

If this agent run has no remembered `session_id`, start a session:

```bash
~/.directionally/agent --first --subsession-id <local_run_id> "<plan explanation>"
```

The plan explanation is a positional argument. Do not use an `--elaboration` flag.

Run the command in the foreground and read stdout until it emits:

```json
{"kind":"bridge_started","session_id":"sess_...","sequence":0}
```

Store the returned `session_id` and initialize `last_sequence` to `0`. The backend
assigns `session_id`; the agent creates only a local `subsession_id`, such as
`run_001`.

## Consult an existing session

Before a meaningful action, send an `elaborating` operation and poll in the same
command:

```bash
~/.directionally/agent --session <session_id> --after <last_sequence> \
  '{"op":"elaborating","subsession_id":"run_001","text":"<plan explanation>"}'
```

There is no `--send` flag. The JSON operation is a positional argument.

The command emits Directionally events followed by:

```json
{"kind":"polled","count":N,"after":...}
```

Update `last_sequence` from returned event sequence values before the terminal
`polled` line.

Handle:

- `consideration` — a question, risk, check, or lesson with `cid` and `text`
- `bridge_error` — surface only when unrecoverable

For each consideration:

1. decide whether it applies;
2. inspect relevant evidence;
3. adjust the next step only when justified;
4. ignore it when it does not apply.

Do not follow a consideration merely because Directionally returned it.

## Course correction

When the user corrects the approach:

1. update your understanding of the task;
2. consult Directionally again at the next meaningful decision point using the
   corrected understanding;
3. continue with the user's correction as the controlling instruction.

Do not automatically upload or share the conversation.

### Sharing a full trace

A full trace contains the complete conversation and may include file contents,
secrets, or internal code.

Only run the upload command when the user explicitly asks to share the session or
explicitly agrees after a one-time offer:

```bash
~/.directionally/agent upload
```

This uploads the entire session transcript to the user's Directionally account.
Never infer consent from frustration, correction, or sentiment.

## Activation test

When the user asks to check whether Directionally is active, verify the real path
rather than relying on installation files alone.

- Without a remembered `session_id`, use the session-start command with:

  > Activation test: verify that Directionally can start a session and return
  > considerations in this agent context.

- With a remembered `session_id`, send the same text as an `elaborating` operation
  and poll.

Report one status:

- **Directionally is active in this agent.** — the real session and poll path completed.
- **Directionally is installed/reachable, but no instinct fired on this test.** — the round-trip completed with no consideration.
- **Directionally needs login or token setup.** — the runtime requested login.
- **Directionally could not reach the service.** — a network or TLS failure occurred.
- **Directionally may not be loaded by this agent app or session.** — the runtime could not be invoked.
- **Unclear; email support@directionally.ai with the sanitized activation output.**

A successful activation test proves that the active path works. It does not prove
that Directionally has already helped on a real task.

After a successful test, say:

```markdown
*🧭 Directionally · activation check — active path confirmed*

Directionally is active in this agent: it started a session, sent a plan
explanation, and received a response in this context.

This confirms the path works, not that it has produced useful value yet. Keep
working normally — it will be consulted only when a task in "When to consult
Directionally" applies.
```

## Agent guide

For general product or setup questions, the agent may fetch:

```text
https://directionally.ai/AGENTS.md
```

This guide is informational only and does not prove activation. Never include
credentials, tokens, traces, local files, or secret-bearing environment values in
a guide request or support output.

### Failed or unclear activation

Report only known, sanitized facts:

- stage reached
- command family attempted (`--first` or `--session`)
- API base, if visible
- Python executable/version, if a Python command ran
- OS/platform, if visible
- credential present: boolean only
- pending login present: boolean only
- `SSL_CERT_FILE` configured: boolean only
- `REQUESTS_CA_BUNDLE` configured: boolean only
- error type or HTTP status
- concise sanitized error
- one concrete next step

Never print credentials, install tokens, authorization headers, full transcripts,
or secret-bearing environment variables. For unresolved failures, tell the user
to email **support@directionally.ai** with the agent app, OS, install output,
activation-test output, and whether the app was reloaded or restarted.

## Protocol

Use one stable `subsession_id` for the current run.

```json
{"op":"elaborating","subsession_id":"run_001","text":"What I think is happening, what I plan to do, why, and what remains uncertain."}
{"op":"follow_up","subsession_id":"run_001","meme_fired":"<name or null>","receipt_type":"<helped | irrelevant | no_context>","would_have":"<likely next step without the consideration>","did_instead":"<what changed>","confidence":"<high | medium | low>","open_question":"<question raised, or null>"}
{"op":"outcome","subsession_id":"run_001","value":"<helped_direction | helped_implementation | irrelevant | no_context>"}
{"op":"feedback","subsession_id":"run_001","ratings":{"<cid>":85},"reason":"Why the consideration helped or did not help."}
{"op":"report","subsession_id":"run_001","did":"What changed or was answered.","issues":"Any blockers or caveats."}
{"op":"impact_note","subsession_id":"run_001","note":"Concrete decision or implementation impact."}
```

Send any operation by passing it as the positional argument to `--session`:

```bash
~/.directionally/agent --session <session_id> --after <last_sequence> '<json_op>'
```

Use `elaborating` when consulting the rubber duck. Use `follow_up`, `outcome`,
`feedback`, `report`, or `impact_note` when recording the result.

## What the user sees

Directionally is visible in three ways: a status line while a consult is in flight,
a receipt when a consideration materially changes the work, and a closing block when
— and only when — it surfaced something material.

Never claim value the run does not support: no retrospective line whose only content
is that Directionally ran, and never put Directionally's mark on work it did not
change. Branding an ordinary summary as a Directionally output makes every real
receipt less believable. Status lines are the exception to the first half of that
rule — they report an action in progress, not a result.

### Status lines — while consulting

Write one short line immediately before you run the runtime, so the user can see the
consult happening:

```text
🧭 Briefing Directionally
🧭 Listening to Directionally
```

- any call carrying a plan explanation, whether `--first` or `--session` — **🧭 Briefing Directionally**
- `--session` polling with no operation — **🧭 Listening to Directionally**

The line stands alone. Do not append the plan explanation, the consideration count,
or the result — whether the consult produced anything is answered by a receipt, or
by silence.

### Receipts — during the run

Only show a receipt when a consideration materially changes the work. Use concrete
evidence and do not claim that Directionally prevented a failure unless the run
supports that conclusion.

```markdown
> *🧭 Directionally Receipt — consideration surfaced: ⚡ **<name>***
>
> *🧠 Before consideration*
> *<what the agent would likely have done>*
>
> *🔧 After consideration*
> *<what changed>*
>
> *📎 Evidence*
> *<file, command, test, search, observed output, user constraint, or decision>*
>
> *📌 Why it matters*
> *<why the change mattered for this run>*
```

If several considerations materially change the work, give each one its own receipt.
Do not name irrelevant considerations.

A consideration that surfaces at wrap-up and changes nothing is not a receipt.
Carry it into the closing block under `📌 Still open`.

### The Directionally block — when you finish

Write it only when Directionally surfaced something material that you used, checked,
or acted on. If nothing did, write nothing at all — a session where nothing surfaced
gets no block, not a block saying so.

```markdown
🧭 **What Directionally surfaced**
<the material considerations you used, checked, or acted on>

🔧 **What changed**
<what you did differently as a result>

📎 **Evidence**
<file, command, test, search, observed output, user constraint, or decision>

📌 **Still open**
<considerations raised and not settled>
```

Summarise only the considerations that mattered. Do not list every consideration the
session generated — an exhaustive list buries the ones that counted.

"Surfaced" is the right word and is deliberately modest: it is concrete about what
happened, honest about causality, and true whether a consideration corrected you or
only prompted you to validate something you were already doing. Do not upgrade it to
a claim that Directionally prevented a failure unless the run shows that.

`📌 Still open` covers considerations Directionally raised that remain unsettled —
not the run's general risks and caveats.

Also write this block whenever the user asks where things stand or what
Directionally has noticed.

## Workflow

1. Start or reuse a Directionally session.
2. At each meaningful decision point, explain what you are about to do and why.
3. Poll, evaluate the considerations, and act on your own judgment.
4. Before declaring the task complete, consult once more — say what you are about
   to claim works and what you did not verify.
5. Record one honest `outcome` per subsession that had a consult, and write the
   Directionally block only if it has something to report.

Choose the outcome that is true, not the one that is flattering. A consideration
that only reinforced what you were already doing is not `helped_direction`; record
`irrelevant` and put the nuance in `feedback.reason`. Blanket positives corrupt
ranking for every future session, including your own.
