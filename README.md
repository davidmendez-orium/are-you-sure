<img src="assets/you-sure-about-that.jpeg" width="360" alt="You sure about that?">

# Are You Sure?

A plugin for **Claude Code** and **Claude Cowork** that makes the agent prove its
claims before it is allowed to finish.

Two halves. An **evidence contract** injected before the agent writes anything, and a
**stop it cannot pass** while a claim is still unearned.

```
YOU SURE ABOUT THAT?  [are-you-sure]

This message claims more than it proved:
  • you wrote "I verified" but ran no test, build, or command this turn — nothing was verified, only read
  • you assert "The root cause is" with no citation — no file:line, command output, or URL a reader could check

For each item, do exactly one of these before you finish:
  1. PROVE it — run the command or open the file, then cite it: file:line, or the
     command together with its real output. Not a paraphrase of the output.
  2. LABEL it — rewrite as INFERRED / ASSUMED / UNVERIFIED and state the one check
     that would settle it.
  3. RETRACT it — cut the claim. A shorter true answer beats a longer confident one.

Softer wording is not a fix: "should be correct" carries the same claim as "is
correct". Change what you actually assert, or go and earn it.
```

The agent sees that instead of stopping, and goes back to earn the claim or label it
honestly.

## Install

```
/plugin marketplace add davidmendez-orium/are-you-sure
/plugin install are-you-sure@are-you-sure
```

`are-you-sure@are-you-sure` reads oddly — the first is the plugin, the second the
marketplace it came from. Both are named after the repo.

Nothing to configure. It is on from the moment it installs, in `heuristic` mode.

Installing prints **"Restart to apply changes"** and means it: hooks load at session
start, so the current session is unaffected. Same after every update.

### Cowork

Same repo, same manifest — Cowork reads the identical `.claude-plugin/marketplace.json`
and accepts a GitHub repo as a marketplace:

**Customize → Plugins → Add marketplace**, enter `davidmendez-orium/are-you-sure`, then
install `are-you-sure` from it. Open the installed plugin to see its skill and its three
hooks, and to enable or disable them individually.

Hooks are Cowork-and-Code only — they're greyed out in Chat, which has no session
lifecycle to hook.

### Platform support

| | Claude Code | Cowork |
|---|---|---|
| The `/are-you-sure` skill | ✅ verified | ✅ same skill format |
| Hooks load and fire | ✅ verified | ⚠️ **unverified** — see below |

Cowork runs hooks inside a VM, and its docs don't state what's on that VM's `PATH`. The
checker is stdlib Python, so `hooks/are-you-sure.sh` resolves `python3`, then `python`,
and **exits 0 quietly** if it finds neither — an inert checker rather than an error on
every turn. To find out which case you're in, run it in a Cowork session:

```
sh "$CLAUDE_PLUGIN_ROOT/hooks/are-you-sure.sh" --selftest
```

`RESULT: OK` means the enforcement half works there. A `FAIL` line means no interpreter,
so only the skill half is live. Run on macOS / Claude Code, it prints:

```
are-you-sure selftest — /opt/homebrew/opt/python@3.14/bin/python3.14 (3.14.6)
  caught: you wrote "I verified" but ran no test, build, or command this turn — nothing was verified, only read
  caught: you assert "The root cause is" having neither opened a file nor cited a source this turn
  contract: 767 chars
  RESULT: OK — the hook works in this environment
```

The interpreter path and version will differ inside Cowork's VM; the `RESULT` line is
the part to read.

## How it works

| Half | Event | What it does | Cost |
|---|---|---|---|
| Prevention | `UserPromptSubmit` | injects the evidence contract **before** generation | ~168 tokens per session |
| Enforcement | `Stop`, `SubagentStop` | inspects what was written, blocks unearned claims | one extra pass, only when it fires |

**Be clear about the ordering, because it decides what this can and cannot do:** a
`Stop` hook fires *after* a message is generated. It cannot suppress a first draft, so
it does not literally prevent the tokens in that draft — it forces a correction. The
half that actually avoids waste is the contract, which lands *before* the agent
writes. Enforcement exists to give the contract teeth: a rule with no consequence
gets ignored by about the third turn.

That is also why the challenge closes by pointing out that hedging honestly the first
time is cheaper than a second pass. The incentive and the instruction agree.

## What it catches

Ordered by signal. The first is the one worth installing for.

| Check | Fires when | Mode |
|---|---|---|
| **Claimed verification** | the message says *verified / confirmed / tested / works / passes* and the turn ran no test, build, or command | `lenient`+ |
| **Uncited conclusion** | a root-cause or safety claim with no `file:line`, no command output, no URL | `heuristic`+ |
| **Unsearched absolute** | *always*, *never*, *nothing else uses this* — claims about everything it didn't find | `strict` |
| **Estimated count** | *several*, *most*, *a few* where enumeration was available | `strict` |
| **Hedge as conclusion** | *should work*, *presumably*, *must be* offered as a finding | `strict` |

"Ran a command" is judged from the transcript, not from the message — the agent
cannot talk its way past this one by describing a test run it never performed.

## Modes

`ARE_YOU_SURE_MODE`, default `heuristic`:

| Mode | Behaviour |
|---|---|
| `off` | fully inert — no injection, no checks |
| `lenient` | claimed verification only. Near-zero false positives |
| `heuristic` | **default.** The above plus uncited conclusions |
| `strict` | everything. Expect it to fire on prose you thought was fine |

| Also | Default | Meaning |
|---|---|---|
| `ARE_YOU_SURE_MAX_PER_TURN` | `1` | challenges per user prompt |
| `ARE_YOU_SURE_MAX_PER_SESSION` | `25` | budget for a whole session |
| `ARE_YOU_SURE_MIN_CHARS` | `120` | shorter messages are treated as conversation |
| `ARE_YOU_SURE_LOG` | `~/.claude/logs/are-you-sure.log` | `off` to disable |

## What it will not do

A hook that blocks the agent from finishing can wedge a session, so the failure
modes are handled deliberately:

- **One challenge per turn.** Tracked by `prompt_id` in `~/.claude/are-you-sure/`, so the
  second stop in a turn always passes.
- **A block it cannot record is a block it does not issue.** Both loop guards read that
  state file, so a counter that fails to persist would repeat forever. Blocking is gated on
  the write succeeding. Measured across 40 consecutive stops on one unchanged message:
  1 block with a writable state dir, 25 (the session cap) if `prompt_id` churned every
  pass, and **0** with the state dir made read-only.
- **Never blocks a question.** If the agent is asking *you* something — trailing `?`,
  or an `AskUserQuestion` / `ExitPlanMode` call — the stop is allowed. Blocking there
  would talk over you.
- **Never blocks small talk.** Under `ARE_YOU_SURE_MIN_CHARS` it does not look.
- **Ignores quoted material.** Fenced blocks and `>` quotes are somebody else's words,
  not the agent's claims.
- **Fails open, always.** Bad JSON, missing transcript, unexpected error — it exits 0
  and lets the agent stop. A broken checker must never cost you a session. The one
  exception: a *missing* transcript still challenges a verification claim, because
  absence of proof is exactly the thing being checked.
- **Never mentions itself.** The challenge tells the agent not to narrate the check or
  apologise. You should see a better answer, not a report about being corrected.

## The skill

The plugin also ships an `/are-you-sure` skill — the doctrine the hook enforces,
readable on its own:

```
/are-you-sure
```

It audits the immediately preceding claim: extracts the claims atomically, grades each
against the evidence actually in the transcript, re-runs the checks that grade low, and
reports `UPHELD` / `REFUTED` / `UNVERIFIABLE` per claim. Useful for auditing a *human's*
claim too, or your own pasted-in reasoning.

Its core is a grading table — RAN, READ, INFERRED, ASSUMED, UNVERIFIED — and the rule
that the grade decides how strongly the claim may be phrased. The two grades that get
skipped are INFERRED and UNVERIFIED, and those are the two that cause the damage: a
missing UNVERIFIED label is how a blocker gets reported as done.

## Tests

```
python3 plugins/are-you-sure/tests/test_are_you_sure.py
```

27 tests, stdlib `unittest`, no dependencies. They drive the hook as a subprocess with
JSON on stdin exactly as Claude Code does, so the contract itself is what's covered —
the loop guards, the handback cases, and the two transcript shapes that cost the most
to learn: **tool results and system reminders both arrive as `type: "user"` rows**, and
treating either as the start of your turn hides every tool call before it, so a
well-evidenced message reads as unevidenced and gets challenged anyway. Real prompts
carry `content` as a string; injections are flagged `isMeta`. All 27 pass on Python
3.14.6 / macOS.

## Repo layout

```
.claude-plugin/marketplace.json         the marketplace catalog
plugins/are-you-sure/
├── .claude-plugin/plugin.json          the plugin manifest
├── hooks/hooks.json                    wires all three events to one shim
├── hooks/are-you-sure.sh               interpreter shim + --selftest
├── hooks/are_you_sure.py               the checker — stdlib only
├── skills/are-you-sure/SKILL.md        the doctrine, and /are-you-sure
└── tests/test_are_you_sure.py
```

One script serves all three events and dispatches on `hook_event_name`. `plugin.json`
deliberately omits `version`, so installs track the git commit SHA and every push
reaches users without a version bump.

If you fork this, note that a hook `command` must be a **string** —
`python3 "${CLAUDE_PLUGIN_ROOT}/hooks/are_you_sure.py"`. The exec-array form
(`["python3", "…"]`) that the plugin reference calls "recommended" is rejected by the
hook loader with `expected string, received array`, and `claude plugin validate` does
**not** catch it: validation passes and the plugin then fails to load at install time.

## License

MIT
