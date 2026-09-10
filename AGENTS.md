# AGENTS.md

Instructions for any AI coding agent working in this repository.

This repository holds `tools/handoff.py`: a local, dependency-free tool that
turns the JSONL transcripts Claude Code and Codex already write into a
`HANDOFF.md` the other agent can resume from. See [docs/handoff.md](docs/handoff.md).

## Ground rules for this project

1. **Standard library only.** No `pip`, no `requirements.txt`, no external
   packages. If a change needs a dependency, the change is wrong.
2. **Agent transcripts are read-only.** Never edit, move, rename, truncate or
   delete anything under `~/.claude/` or `~/.codex/`. Read, and only read.
3. **The LLM is optional.** Every command must produce a useful result with no
   endpoint configured and no network. An LLM failure falls back to the
   deterministic handoff and still exits 0.
4. **Order of truth: Git and the filesystem, then the transcript, then any
   interpretation.** Never invert it. A claim in a transcript is not evidence
   that code exists.
5. **Never write a large diff into `HANDOFF.md`.** The next agent shares the
   checkout and can run `git diff`. The handoff is a map, not a copy.
6. Run the checks before finishing:

   ```bash
   python3 -m py_compile tools/handoff.py
   python3 -m unittest discover -s tests -t .
   ```

7. Fixtures in `tests/` are synthetic. Never commit a real transcript, a
   credential, or anything from `.handoff/`.

## Agent handoff

Before continuing an existing task:

1. Read `HANDOFF.md` when it exists.
2. Inspect `git status` and `git diff`.
3. Preserve unfinished work from previous agents.
4. Read `.handoff/conversation-tail.md` when additional historical context is required.
5. Treat repository source and tests as authoritative over historical handoff text.

Before ending substantial work:

1. Update or generate the handoff.
2. Record completed work, decisions, blockers and next steps.
3. Do not paste large diffs into the handoff; the repository is the source of truth.

### Generating one

```bash
python3 tools/handoff.py snapshot claude        # planned switch of agent
python3 tools/handoff.py recover  claude        # the previous session died
python3 tools/handoff.py snapshot codex  --ai   # add --ai when an LLM is configured
```

Add `--repo PATH` to act on a different checkout, or export `HANDOFF_REPO`.

`HANDOFF.md` and `.handoff/` are local artifacts excluded through
`.git/info/exclude`. Do not commit them, and do not add them to `.gitignore`.
