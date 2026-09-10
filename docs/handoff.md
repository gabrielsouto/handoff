# Handoff between Claude Code and OpenAI Codex

`tools/handoff.py` turns the JSONL transcripts that Claude Code and Codex already
write into a `HANDOFF.md` the other agent can pick up, in the same checkout,
without losing what was already done.

It exists for one specific moment: **a session dies mid-task** — quota ran out,
the window was closed, the context was lost — and there was no chance to ask for
a summary. The transcript is still on disk. This tool reads it.

- No daemon, no server, no database, no MCP, no cron.
- Python 3 standard library only. Nothing to install.
- The agent transcripts are **read-only**, always.
- The LLM is **optional and off by default**. There may be no integration at
  all; nothing here contacts the network until you configure an endpoint.
- One copy serves every project: `--repo PATH` points it at any checkout.

---

## 1. Order of truth

```
1. Git and the current filesystem
2. The real session transcript
3. Any interpretation (yours, or the model's)
```

Never inverted. A sentence in a transcript is not proof that code exists. Only
the repository, its history and its tests can confirm that. The deterministic
handoff therefore writes `Not automatically determined` rather than guessing,
and the AI prompt is instructed to mark anything uncertain as `não confirmado`.

---

## 2. Where the sessions live

### Claude Code

```
~/.claude/projects/<project-slug>/<session-uuid>.jsonl
```

The slug is the project path with every non-alphanumeric character replaced by
`-`, so `/var/www/html/soutog/pgedigital` becomes `-var-www-html-soutog-pgedigital`
and `d:\Vida\Profissional\handoff` becomes `d--Vida-Profissional-handoff`.

Record shape (Claude Code 2.x):

| `type`                                  | meaning                                             |
| --------------------------------------- | --------------------------------------------------- |
| `user` / `assistant`                    | conversation; `message.content` is a list of blocks |
| `system` + `subtype: compact_boundary`  | context compaction                                   |
| `summary`, `isCompactSummary: true`     | compaction summary text                              |
| `attachment`, `file-history-*`, `mode`, `ai-title`, `bridge-session`, ... | bookkeeping, ignored |

Content blocks: `text`, `thinking`, `tool_use`, `tool_result`, `image`.
Conversation records also carry `cwd`, `gitBranch`, `sessionId`, `timestamp`
and `uuid`. A failed tool shows up as `tool_result` with `"is_error": true`.
`thinking` blocks (opaque signatures) and `image` blocks (base64) are never
copied into the evidence.

### Codex

```
~/.codex/sessions/YYYY/MM/DD/rollout-<ISO>-<session-uuid>.jsonl
```

Every record is `{timestamp, ordinal, type, payload}`:

| `type`              | payload of interest                                                         |
| ------------------- | --------------------------------------------------------------------------- |
| `session_meta`      | `session_id`, `cwd`, `originator`, `cli_version`                            |
| `turn_context`      | `cwd`, `workspace_roots`, `model`                                            |
| `response_item`     | `message` (roles user/assistant/developer), `custom_tool_call`, `custom_tool_call_output`, `function_call*`, `reasoning` |
| `event_msg`         | `item_completed` with items `UserMessage`, `AgentMessage`, `FileChange`, `WebSearch`, `McpToolCall`, `ContextCompaction` |
| `compacted`         | `replacement_history` — the context was compacted                            |
| `token_usage_record`, `world_state` | accounting, ignored                                         |

Two details that matter:

- **File edits only appear in `event_msg/item_completed` items of type
  `FileChange`**, so those are read from there while messages are read from
  `response_item` — otherwise every turn would be counted twice.
- **Codex injects machine-written blocks as user-role messages**
  (`<recommended_plugins>`, `<codex_internal_context>`, `<environment_context>`).
  A message that is nothing but complete XML elements was not typed by a human,
  so it is dropped. In a real 112 MiB session this separated 6 genuine user
  requests from 26 injected blocks.

The parser never assumes a format it has not seen: unknown record types simply
produce no event instead of raising.

---

## 3. How a session is tied to this repository

In order of strength:

1. **`cwd` recorded inside the session** — checked by reading only the head of
   the file. Match → `confirmed`; different checkout → `foreign`.
2. **Directory name hint** (Claude's project slug) → `hinted`.
3. Nothing found → `unknown`.

Automatic selection takes the most recent `confirmed` session, then the most
recent `hinted` one. It **never** silently picks a `foreign` session: if every
candidate belongs to another checkout, the command fails and tells you to choose
explicitly. Ordering always uses the real `mtime`, never the date in the path.

Override discovery whenever you need to:

```bash
python3 tools/handoff.py recover claude --session 4114df3c-a38c-49c9-895a-baaab587aab2
python3 tools/handoff.py snapshot codex  --session-file ~/.codex/sessions/2026/09/09/rollout-....jsonl
```

`--session` accepts a unique prefix.

---

## 4. Files produced

```
<repo>/
├── HANDOFF.md                      # what the next agent reads
└── .handoff/
    ├── config.json                 # local configuration (created by `init`)
    ├── session.json                # snapshot metadata + streamed SHA256
    ├── git-state.txt               # readable Git snapshot
    ├── conversation-tail.md        # selected transcript evidence
    ├── ai-input-preview.md         # exactly what would go to the LLM (only with --ai)
    └── history/
        └── 2026-09-09_174530_claude_4114df3c.md
```

All of it is local. `init` adds `/HANDOFF.md` and `/.handoff/` to
`.git/info/exclude` — **never** to `.gitignore`, which stays yours — and does so
idempotently, preserving whatever rules were already there.

The leading slash matters: an unanchored `HANDOFF.md` matches a file of that name
at *any* depth, and on a case-insensitive filesystem it also swallows
`docs/handoff.md`. If an older run left the unanchored form behind, `init`
rewrites those two lines in place and touches nothing else.

---

## 5. One copy, every project

Keep this folder as the only copy of the tool. Point it at whatever checkout you
are working in:

```bash
python3 tools/handoff.py doctor --repo /var/www/html/soutog/pgedigital
python3 tools/handoff.py recover claude --repo /var/www/html/soutog/pgedigital
```

`--repo` works before or after the subcommand. To stop retyping the path:

```bash
export HANDOFF_REPO=/var/www/html/soutog/pgedigital   # Linux / macOS / Git Bash
$env:HANDOFF_REPO = "D:\Vida\Profissional\htdocs\ementa"   # PowerShell
```

Precedence: `--repo` > `$HANDOFF_REPO` > the current directory. An explicit path
that is not a directory is a usage error (exit 2), never a silent fallback.

There are three thin wrappers in this folder so the command is shorter —
`handoff` (sh), `handoff.cmd`, `handoff.ps1`. They forward every argument:

```bash
./handoff recover claude --repo /path/to/project
.\handoff.ps1 status
```

`HANDOFF.md` and `.handoff/` are always written **into the target repository**,
which is where the next agent will look for them. Nothing is written here.

Running from inside the project also still works — no `--repo` needed:

```bash
cd /var/www/html/soutog/pgedigital
python3 ~/tools/handoff/tools/handoff.py recover claude
```

---

## 6. Commands

```bash
python3 tools/handoff.py init                  # once per repository
python3 tools/handoff.py doctor                # full diagnosis
python3 tools/handoff.py status                # short summary
python3 tools/handoff.py snapshot claude       # planned handover
python3 tools/handoff.py snapshot codex  --ai
python3 tools/handoff.py recover  claude --ai  # the session died
python3 tools/handoff.py recover  codex
python3 tools/handoff.py consolidate --ai      # redo the summary, same snapshot
python3 tools/handoff.py history               # list archived handoffs
python3 tools/handoff.py show                  # print HANDOFF.md
```

`snapshot` and `recover` run the same pipeline; the two names exist so the
intent is obvious at the moment you need it. `consolidate` reuses the stored
`conversation-tail.md` and only refreshes the Git facts, so it is cheap to
re-run after the working tree changes.

Flags on `snapshot` / `recover`: `--session`, `--session-file`, `--ai`,
`--dry-run`. On every command: `--repo PATH` and `--verbose` (debug output and
full tracebacks), accepted before or after the subcommand.

`--ai` is safe to pass even with no endpoint configured: it explains that it is
skipping and produces the deterministic handoff.

Exit codes: `0` success, `1` general error, `2` invalid usage/configuration,
`3` session not found, `4` Git error, `5` LLM error. An LLM failure during
`--ai` is **not** exit 5: it warns and falls back to the deterministic handoff
with exit 0.

---

## 7. Configuration

`.handoff/config.json`, created by `init`, never overwritten afterwards:

```json
{
  "llm": {
    "enabled": true,
    "base_url": "",
    "model": "",
    "api_key_env": "HANDOFF_LLM_API_KEY",
    "timeout_seconds": 120,
    "max_input_chars": 120000,
    "temperature": 0.1
  },
  "evidence": {
    "first_events": 20,
    "last_events": 60,
    "max_errors": 30,
    "max_tool_events": 80,
    "max_compactions": 20,
    "max_user_messages": 40,
    "max_text_chars": 2000,
    "max_tool_text_chars": 1200
  }
}
```

`base_url` and `model` ship **empty**: out of the box there is no LLM, and that
is a supported, fully working configuration. Filling both in is what turns the
AI step on. `enabled` is only an explicit opt-out — set it to `false` to refuse
the AI step even when `--ai` is passed.

Since `.handoff/` is excluded from Git, this file can hold machine-specific
settings safely. Environment overrides win over the file:

```bash
HANDOFF_LLM_BASE_URL=http://10.120.191.20:8000   # any OpenAI-compatible endpoint
HANDOFF_LLM_MODEL=DeepSeek-V4-Flash-0731
HANDOFF_LLM_API_KEY=...                          # optional; never stored on disk
HANDOFF_LLM_TIMEOUT=120
```

The generated file carries `_example_base_url` and `_example_model` keys as a
reminder of the internal DeepSeek endpoint; they are documentation, not settings.

No API key ever appears in the code, in `config.json`, in the terminal output or
in `ai-input-preview.md`.

---

## 8. The optional LLM

**There may be no LLM at all, and that is fine.** With none configured:

- `doctor` reports `status: off` and returns immediately — it does not probe,
  so there is no timeout to sit through;
- `--ai` says why it is skipping and writes the deterministic handoff, exit 0;
- `--ai --dry-run` still writes `ai-input-preview.md`, so you can inspect what
  *would* be sent before ever choosing an endpoint;
- nothing opens a socket.

When one *is* configured, the client speaks the OpenAI-compatible subset
(`GET /v1/models`, `POST /v1/chat/completions`) over `urllib` — no `requests`,
no `httpx`, no `openai` package.

**The model never receives the raw transcript.** A 112 MiB session is reduced to
an Evidence Pack of a few dozen KiB in one streaming pass:

- the first events (original goal, constraints, initial request);
- the last events, kept in a bounded `deque` — the answer to "where did we stop?";
- the earliest and most recent user messages;
- failed tool calls, exceptions and non-zero exits;
- compaction summaries, which are already condensed history;
- file-mutating tool calls and the paths they touched.

Everything is truncated with a visible marker
(`[tool result truncated: original length 183204 chars]`) so nothing looks
complete when it is not.

Before any request, the full payload is written to `.handoff/ai-input-preview.md`
so you can audit it. To generate the preview without calling anything:

```bash
python3 tools/handoff.py recover claude --ai --dry-run
```

In AI mode the model writes only the ten analysis sections. The header, `Git
State`, `Evidence` and `Resume Instructions` are always generated deterministically,
so `HANDOFF.md` has the same skeleton either way.

---

## 9. Security

Redaction runs over everything before it is written or sent: `Authorization`
headers, `Cookie` headers, `api_key=` / `token=` / `password=` / `secret=`
assignments, `sk-`, `sk-ant-`, `ghp_`, `github_pat_`, `xox*`, `AKIA`/`ASIA`,
Google API keys, JWTs and `PRIVATE KEY` blocks, all replaced with `[REDACTED]`.
The key name survives so the context stays readable; the value does not.

Tool calls that read a file looking like a credential store
(`.credentials.json`, `auth.json`, `.env`, `id_rsa`, `*.pem`, `.netrc`, ...)
have their payload dropped entirely rather than redacted.

The count is reported (`Redacted 3 possible secret(s).`) and recorded in
`session.json`.

**This is defence in depth, not DLP.** It reduces accidental exposure; it does
not guarantee a transcript is clean. Read `ai-input-preview.md` the first time
you point this at a new endpoint.

---

## 10. Daily flow

### Planned switch

```bash
python3 tools/handoff.py snapshot claude --ai
python3 tools/handoff.py snapshot claude --ai --repo /path/to/project   # from elsewhere
```

Drop `--ai` if no LLM is configured; the handoff is still produced.

Then open Codex and say:

```
Leia AGENTS.md e HANDOFF.md e continue o trabalho atual.
Inspecione o Git e o código antes de fazer mudanças.
```

### Emergency — the session died without a handoff

```bash
python3 tools/handoff.py recover claude --ai
```

Then continue in Codex. Reverse the agent name to go the other way:

```bash
python3 tools/handoff.py snapshot codex --ai
```

### Re-summarize without re-reading the transcript

```bash
python3 tools/handoff.py consolidate --ai
```

---

## 11. Troubleshooting

**`error: not inside a Git repository`** — the tool derives the repository root
from `git rev-parse --show-toplevel`. Run it from inside the checkout. No path
is ever hardcoded, so the same script works in any repository.

**`no Claude session could be tied to <repo>`** — the sessions on this machine
record a different `cwd`. Check with `doctor`, then pass `--session` or
`--session-file`. This refusal is deliberate: importing another project's
session would produce a confidently wrong handoff.

**`all belong to other checkouts`** — same cause, stated explicitly, with the
`cwd` of the most recent candidate so you can tell whether it is the one you
meant.

**`reachable: no` under AI consolidation** — the endpoint is unreachable from
this machine (commonly: an internal address seen only from the corporate
network). Everything except `--ai` still works, and `--ai` itself falls back to
the deterministic handoff with a warning.

**`N invalid JSONL line(s) ignored`** — normal for a transcript whose last line
was still being written when the process died. A bad line never aborts the pass;
the count is reported and stored in `session.json`.

**Detached HEAD** — reported as `(detached: <ref>)`, not an error.

**The handoff looks thin** — without `--ai` the tool refuses to infer decisions
and pending work. That is the design. Use `--ai`, or read
`.handoff/conversation-tail.md`, which holds the raw evidence.

---

## 12. Tests

```bash
python3 -m py_compile tools/handoff.py
python3 -m unittest discover -s tests -t .
```

118 tests, standard library only. Fixtures are synthetic and reproduce only the
record shapes observed in real transcripts — no real session is committed. The
suite covers normalization for both agents, redaction, truncation, evidence
selection limits, atomic writes, streaming SHA256, malformed JSONL, Git porcelain
parsing, `.git/info/exclude` idempotency, `AGENTS.md` handling, history naming,
handoff rendering, `--repo` resolution, the LLM on/off gate, and the LLM client
against a local `http.server` stub.

---

## 13. Deliberately out of scope

Vector embeddings, semantic search over history, an HTTP server, MCP, a web UI,
multi-user support, cross-machine sync, a database, watchers, automatic
Claude/Codex hooks, a VS Code plugin, any background service.

When reliability and features conflict here, reliability wins.
