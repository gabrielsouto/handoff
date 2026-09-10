# handoff

*[Leia em português](README.md)*

**A session dies mid-task. The transcript is still on disk. That's where the handoff comes from.**

`tools/handoff.py` turns the JSONL transcripts that Claude Code, OpenAI Codex
and Gemini CLI already write to disk into a single `HANDOFF.md`: a factual
summary, anchored in the real state of Git, that the next agent — or you,
hours later — uses to pick the work back up in the same checkout without
losing what was already done.

No daemon. No server. No database. No dependencies to install. One Python
file, standard library only.

```
Claude works for hours
     │
     ▼
session JSONL on disk         (Claude Code / Codex / Gemini CLI already write this)
     │
     ▼
tools/handoff.py
     │
     ├── current Git state
     ├── selected transcript evidence
     └── optional LLM consolidation
     │
     ▼
HANDOFF.md
     │
     ▼
Codex reads it and continues the work
```

---

## Why

To pick an AI coding session back up after a quota limit, a closed window or
a switch of agent, there are usually two options:

- write and maintain a `HANDOFF.md` by hand; or
- stand up a full memory platform — a server, MCP, a vector database, a daemon.

This tool sits in between. It reads the transcripts the agents *already
write* (no hooks, no interception, no new state to maintain) and builds the
handoff on its own, in seconds, entirely on your machine.

## Guarantees

- **Read-only, always.** Nothing under `~/.claude/`, `~/.codex/` or
  `~/.gemini/` is ever edited, moved, renamed or deleted.
- **Git is the source of truth, always.** Order of evidence: Git and the
  filesystem, then the real transcript, then any interpretation — never the
  other way around. A sentence in a transcript is not proof that code exists.
- **Works with zero configuration and zero network access.** The optional LLM
  consolidation step is off by default; every command produces a complete,
  useful `HANDOFF.md` without it.
- **Never sends the raw transcript anywhere.** A 100+ MiB session is reduced
  to a bounded, redacted Evidence Pack (a few dozen KiB) before anything is
  written to disk or considered for an LLM call — and you can inspect that
  exact payload (`ai-input-preview.md`) before it's ever sent.
- **One copy serves every project.** Point the same script at any checkout
  with `--repo` or `$HANDOFF_REPO`; nothing needs to be installed per-repo.
- **Streams; never loads a whole transcript into memory.** A real 112 MiB
  Codex session parses in ~2.4 s with a ~31 MiB peak Python heap.
- **Agent memory is indexed, never ingested.** `HANDOFF.md` lists what exists
  (Claude's memory directory, Codex's SQLite stores, `CLAUDE.md`/`GEMINI.md`)
  and how fresh it is, but never reads the content — it's another agent's
  past interpretation, not evidence.

## Supported agents

| Agent           | Transcript location                                          | Status |
| --------------- | -------------------------------------------------------------- | ------ |
| Claude Code     | `~/.claude/projects/<slug>/<session-uuid>.jsonl`              | Validated against real sessions on disk |
| OpenAI Codex    | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`                | Validated against a real 112 MiB session (~2.4 s to parse, ~31 MiB peak heap) |
| Gemini CLI      | `~/.gemini/tmp/<project-slug>/chats/session-*.jsonl`          | Built from the CLI's own (unminified) source + a partial real probe; not yet run against a full authenticated session — see [docs/handoff.md](docs/handoff.md#gemini-cli) |

Adding another agent means writing one more adapter that emits the same
normalized event shape; nothing else in the tool changes.

---

## Requirements

- Python 3 (standard library only — nothing to `pip install`)
- Git
- Claude Code, Codex and/or Gemini CLI, for whichever agents you use

## Install

```bash
git clone https://github.com/gabrielsouto/handoff.git
```

Keep this folder wherever you like: it doesn't need to live inside the
projects it works on. Optionally, run `init` once in each repository you plan
to use it with, from inside that repository:

```bash
cd /path/to/your/project
python3 /path/to/handoff/tools/handoff.py init
```

`init` creates `.handoff/`, writes a default `.handoff/config.json`, adds
`/HANDOFF.md` and `/.handoff/` to `.git/info/exclude` (never to your
`.gitignore`, idempotently), and appends an **Agent handoff** section to
`AGENTS.md` if one already exists (it never creates one for you).

Three thin wrappers in this repo shorten the call, wherever you keep the
tool: `./handoff` (sh), `handoff.cmd` and `handoff.ps1`. All of them forward
every argument to `tools/handoff.py`:

```bash
./handoff recover claude --repo /path/to/your/project
```

---

## Quick start

```bash
python3 tools/handoff.py doctor       # what the tool sees on this machine
python3 tools/handoff.py status       # summary of the repository and the sessions
```

**A planned agent switch**, when you're about to stop and hand off on purpose:

```bash
python3 tools/handoff.py snapshot claude --ai
```

Then, in Codex:

> Read AGENTS.md and HANDOFF.md and continue the current work. Inspect Git
> and the code before making any change.

**An emergency**, when the session died before you could ask for a summary:

```bash
python3 tools/handoff.py recover claude --ai
```

With no LLM endpoint configured, just drop the `--ai`: the `HANDOFF.md` comes
out complete either way, only without the model's synthesis of the ten
analysis sections.

---

## Commands

```bash
python3 tools/handoff.py init                  # once per repository
python3 tools/handoff.py doctor                # full diagnosis
python3 tools/handoff.py status                # short summary
python3 tools/handoff.py snapshot claude       # planned handover
python3 tools/handoff.py snapshot codex  --ai
python3 tools/handoff.py recover  claude --ai  # the session died
python3 tools/handoff.py recover  gemini
python3 tools/handoff.py consolidate --ai      # redo the summary, same snapshot
python3 tools/handoff.py history               # list archived handoffs
python3 tools/handoff.py show                  # print HANDOFF.md
```

`snapshot` and `recover` run the exact same pipeline; the two names exist so
the intent is obvious at the moment you reach for one. Every command accepts
`--repo PATH` and `--verbose`, before or after the subcommand;
`snapshot`/`recover` also accept `--session ID`, `--session-file PATH`,
`--ai` and `--dry-run`.

```
$ python3 tools/handoff.py --help
usage: handoff.py [-h] [--repo PATH] [--verbose]
                  {init,doctor,status,snapshot,recover,consolidate,history,show} ...

Generate a handoff between Claude Code, OpenAI Codex and Gemini CLI from the
transcripts they already write.

positional arguments:
  {init,doctor,status,snapshot,recover,consolidate,history,show}
    init                create .handoff/, config and git exclude rules
    doctor              diagnose repository, agents and AI endpoint
    status              short summary of repo, handoff and sessions
    snapshot            capture the current state (normal use)
    recover             rebuild a handoff after a session died
    consolidate         rebuild HANDOFF.md from the existing snapshot
    history             list archived handoffs
    show                print the current HANDOFF.md
```

Exit codes: `0` success, `1` general error, `2` invalid usage, `3` session
not found, `4` Git error, `5` LLM error (`--ai` itself never returns 5 — an
LLM failure falls back to the deterministic handoff with exit 0).

---

## How a session gets picked

In order of strength, the working directory **recorded inside the session
itself** (Claude and Codex carry it at the head of the transcript; Gemini, in
its `.project_root` marker file) beats a **directory-name hint**, which in
turn beats no evidence at all. Automatic selection takes the most recent
confirmed session, then the most recent hinted one, and **never** silently
assumes a session from a different checkout: if every candidate belongs to
another project, the command refuses and asks you to choose explicitly.

```bash
python3 tools/handoff.py recover claude --session 4114df3c-a38c-49c9
python3 tools/handoff.py recover codex  --session-file ~/.codex/sessions/2026/09/09/rollout-....jsonl
```

## What gets written

```
<your-project>/
├── HANDOFF.md                      # what the next agent reads
└── .handoff/
    ├── config.json                 # local config (created by `init`, never overwritten)
    ├── session.json                # snapshot metadata + streamed SHA256 of the transcript
    ├── git-state.txt               # readable Git snapshot
    ├── conversation-tail.md        # selected transcript evidence, truncated and labeled
    ├── ai-input-preview.md         # exactly what would be sent to the LLM (only with --ai)
    └── history/
        └── 2026-09-09_174530_claude_4114df3c.md
```

All of it stays local and out of Git via `.git/info/exclude`. `HANDOFF.md`
never contains a full diff — the next agent shares the checkout and can run
`git diff`; the handoff is a map, not a copy.

An **Agent Memory** section is also always included, listing whatever agent
memory the tool found (existence and freshness, never content) — see
[docs/handoff.md](docs/handoff.md#10-agent-memory).

## The optional LLM

By default there is no LLM integration at all, and nothing here opens a
socket. To turn it on, fill in `base_url` and `model` in
`.handoff/config.json` (any `/v1/chat/completions` endpoint compatible with
the OpenAI API) or export `HANDOFF_LLM_BASE_URL`; to keep it off for good,
set `"enabled": false`.

Passing `--ai` is always safe: with nothing configured, or with the endpoint
down, it explains why and falls back to the deterministic handoff instead of
bringing the whole command down with it.

Even with an endpoint configured, the model still never sees the raw
transcript. A single streaming pass builds the Evidence Pack first — bounded,
truncated and with secrets redacted — and `--dry-run` shows you the exact
payload before anything is sent.

## Security notes

Best-effort redaction runs over everything before it's written or sent:
`Authorization`/`Cookie` headers, `api_key=`/`token=`/`password=`/`secret=`
assignments, `sk-…`, `sk-ant-…`, `ghp_…`, AWS/Google key patterns, JWTs and
`PRIVATE KEY` blocks all become `[REDACTED]`. Files that look like a
credential store (`.env`, `auth.json`, `id_rsa`, `*.pem`, …) have their
content dropped outright rather than redacted.

This is defense in depth, not a DLP guarantee. The first time you point the
tool at a new endpoint, read `.handoff/ai-input-preview.md`.

---

## Tests

```bash
python3 -m py_compile tools/handoff.py
python3 -m unittest discover -s tests -t .
```

156 tests, standard library only (`unittest` + a local `http.server` stub for
the LLM client). Fixtures are synthetic, built from the record shapes
actually observed in real transcripts — no real session is ever committed.

## Documentation

[docs/handoff.md](docs/handoff.md) is the full reference: the exact JSONL
record shapes for all three agents (including Gemini's `$set`/`$rewindTo`
patch-log semantics), the session-to-repository matching rules, every file
this tool writes, the complete configuration schema, and a troubleshooting
section for the errors you'll actually hit.

## Known limitations

- The Gemini adapter is built from the CLI's own shipped source and a
  session that errored out before any model reply — not from a complete,
  successful, authenticated round trip. Treat it as less battle-tested than
  the Claude/Codex adapters until it's been run against a real session.
- Redaction is heuristic, not exhaustive.
- `AGENTS.md` integration only appends to a file that already exists; it
  never creates one.

## Deliberately out of scope

Vector embeddings, semantic search over history, an HTTP server, MCP, a web
UI, multi-user support, cross-machine sync, a database, watchers, automatic
agent hooks, a VS Code plugin, any background service. When reliability and
a new feature conflict, reliability wins.

## License

[MIT](LICENSE) © 2026 Gabriel Souto.
