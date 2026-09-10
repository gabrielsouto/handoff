#!/usr/bin/env python3
"""Local, dependency-free handoff between coding agents (Claude Code / OpenAI Codex).

Reads the JSONL transcripts the agents already write, combines them with the
current Git state, and produces a HANDOFF.md the next agent can use to resume
work in the same checkout.

Design rules:
  * Python 3 standard library only.
  * Agent transcripts are read-only, always. Never edited, moved or copied whole.
  * Order of truth: Git/filesystem > real transcript > LLM interpretation.
  * The optional LLM only reorganizes an already-built Evidence Pack.
  * Everything keeps working when the LLM is unavailable.

Usage:
    python3 tools/handoff.py <command> [options]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NO_SESSION = 3
EXIT_GIT = 4
EXIT_LLM = 5

HANDOFF_DIRNAME = ".handoff"
HANDOFF_FILENAME = "HANDOFF.md"
CONFIG_FILENAME = "config.json"
SESSION_META_FILENAME = "session.json"
GIT_STATE_FILENAME = "git-state.txt"
CONVERSATION_TAIL_FILENAME = "conversation-tail.md"
AI_PREVIEW_FILENAME = "ai-input-preview.md"
HISTORY_DIRNAME = "history"

# Anchored to the repository root on purpose. An unanchored "HANDOFF.md" matches
# any file of that name at any depth, and on a case-insensitive filesystem it
# also swallows docs/handoff.md.
EXCLUDE_RULES = ("/" + HANDOFF_FILENAME, "/" + HANDOFF_DIRNAME + "/")
LEGACY_EXCLUDE_RULES = {
    HANDOFF_FILENAME: "/" + HANDOFF_FILENAME,
    HANDOFF_DIRNAME + "/": "/" + HANDOFF_DIRNAME + "/",
}

AGENT_CLAUDE = "claude"
AGENT_CODEX = "codex"
AGENT_GEMINI = "gemini"
AGENTS = (AGENT_CLAUDE, AGENT_CODEX, AGENT_GEMINI)
AGENT_LABELS = {AGENT_CLAUDE: "Claude", AGENT_CODEX: "Codex", AGENT_GEMINI: "Gemini"}

# Normalized event kinds.
KIND_USER = "user_message"
KIND_ASSISTANT = "assistant_message"
KIND_TOOL_CALL = "tool_call"
KIND_TOOL_RESULT = "tool_result"
KIND_TOOL_ERROR = "tool_error"
KIND_COMPACTION = "compaction"
KIND_METADATA = "session_metadata"
KIND_OTHER = "other"

# Tool names that imply a file mutation.
MUTATING_TOOL_NAMES = {
    "write", "edit", "multiedit", "notebookedit", "create", "delete",
    "str_replace_editor", "str_replace_based_edit_tool", "apply_patch",
    "patch", "update_file", "write_file", "edit_file", "filechange",
}
MUTATION_KEYWORDS = ("write", "edit", "patch", "create", "delete", "replace")

# The LLM is entirely optional and OFF by default: there may be no endpoint at
# all. Nothing contacts the network until base_url and model are filled in and
# "enabled" is true (or HANDOFF_LLM_BASE_URL is exported).
DEFAULT_CONFIG: Dict[str, Any] = {
    "llm": {
        "enabled": True,
        "base_url": "",
        "model": "",
        "api_key_env": "HANDOFF_LLM_API_KEY",
        "timeout_seconds": 120,
        "max_input_chars": 120000,
        "temperature": 0.1,
        "_example_base_url": "http://10.120.191.20:8000",
        "_example_model": "DeepSeek-V4-Flash-0731",
        "_help": ("Optional. Filling in base_url and model of any OpenAI-compatible "
                  "endpoint is what turns --ai on; exporting HANDOFF_LLM_BASE_URL does "
                  "the same. Set enabled=false to refuse the AI step even with --ai. "
                  "With no endpoint every command still works and produces a "
                  "deterministic handoff."),
    },
    "evidence": {
        "first_events": 20,
        "last_events": 60,
        "max_errors": 30,
        "max_tool_events": 80,
        "max_compactions": 20,
        "max_user_messages": 40,
        "max_text_chars": 2000,
        "max_tool_text_chars": 1200,
    },
}

# Files whose content must never be forwarded anywhere.
CREDENTIAL_PATH_HINTS = (
    ".credentials.json", "auth.json", ".env", "id_rsa", "id_ed25519",
    ".pem", ".p12", ".pfx", ".netrc", ".npmrc", ".pgpass", "credentials",
)

VERBOSE = False


def log_verbose(message: str) -> None:
    if VERBOSE:
        sys.stderr.write("debug: %s\n" % message)


def warn(message: str) -> None:
    sys.stderr.write("WARNING: %s\n" % message)


class HandoffError(Exception):
    """A user-facing error: reported without a stack trace."""

    def __init__(self, message: str, code: int = EXIT_ERROR) -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------
# Small utilities
# --------------------------------------------------------------------------


def now_iso() -> str:
    """Local-timezone ISO 8601 timestamp, e.g. 2026-09-09T17:42:31-03:00."""
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def ts_to_iso(value: Any) -> Optional[str]:
    """Best-effort conversion of a transcript timestamp to local ISO 8601."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            seconds = float(value)
            if seconds > 1e11:  # milliseconds
                seconds /= 1000.0
            return datetime.fromtimestamp(seconds).astimezone().replace(microsecond=0).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone().replace(microsecond=0).isoformat()


def mtime_iso(path: Path) -> str:
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return "unknown"
    return datetime.fromtimestamp(stamp).astimezone().replace(microsecond=0).isoformat()


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024.0 or unit == "TiB":
            if unit == "B":
                return "%d B" % int(size)
            return "%.1f %s" % (size, unit)
        size /= 1024.0
    return "%d B" % num_bytes


def short_id(session_id: Optional[str], length: int = 8) -> str:
    if not session_id:
        return "unknown"
    return str(session_id)[:length]


_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename_component(value: Optional[str], fallback: str = "unknown") -> str:
    """Make a string safe to use as exactly one filesystem path component.

    session_id and agent both end up in a filename (see archive_handoff). Both
    are read from the transcript - agent is normally fixed by this tool, but
    session_id comes straight from JSON an attacker-crafted --session-file
    could control. Strip anything but letters, digits, dot, dash and
    underscore, and refuse a result that collapses to '.' or '..' - either
    one is a directory reference, not a filename, even with no slash in it.
    """
    text = _UNSAFE_FILENAME_RE.sub("_", str(value or "")).strip("_")
    if not text or text in (".", ".."):
        return fallback
    return text


def short_head(head: Optional[str], length: int = 12) -> str:
    """Abbreviate a real commit sha; leave placeholders like '(no commits yet)' intact."""
    if not head:
        return "unknown"
    text = str(head)
    return text[:length] if re.fullmatch(r"[0-9a-f]{7,40}", text) else text


def truncate_text(text: Optional[str], limit: int, label: str = "text") -> str:
    """Truncate with an explicit marker so nothing looks silently complete."""
    if not text:
        return ""
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n[%s truncated: original length %d chars]" % (label, len(text))


def collapse_blank_lines(text: str, max_consecutive: int = 2) -> str:
    return re.sub(r"\n{%d,}" % (max_consecutive + 1), "\n" * max_consecutive, text)


def atomic_write(path: Path, content: str) -> None:
    """Write to a temp file in the same directory, fsync, then os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle_fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, str(path))
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> Optional[str]:
    """Streaming SHA256 - never loads the transcript into memory."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        log_verbose("sha256 failed for %s: %s" % (path, exc))
        return None
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def iter_jsonl(path: Path) -> Iterator[Tuple[Optional[Dict[str, Any]], bool]]:
    """Yield (record, ok) per line, streaming. A bad line never aborts the session."""
    try:
        handle = open(path, "r", encoding="utf-8", errors="replace")
    except OSError as exc:
        raise HandoffError("cannot read transcript %s: %s" % (path, exc))
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (ValueError, RecursionError):
                yield None, False
                continue
            if not isinstance(record, dict):
                yield None, False
                continue
            yield record, True


def normalize_path(value: Optional[str]) -> str:
    """Comparable path form: forward slashes, no trailing slash, case-folded on Windows."""
    if not value:
        return ""
    text = str(value).strip().replace("\\", "/")
    while len(text) > 1 and text.endswith("/"):
        text = text[:-1]
    if os.name == "nt" or re.match(r"^[A-Za-z]:/", text):
        text = text.lower()
    return text


def path_is_within(candidate: str, root: str) -> bool:
    candidate = normalize_path(candidate)
    root = normalize_path(root)
    if not candidate or not root:
        return False
    return candidate == root or candidate.startswith(root + "/")


# --------------------------------------------------------------------------
# Redaction (best effort - explicitly not a complete DLP solution)
# --------------------------------------------------------------------------

REDACTED = "[REDACTED]"

_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN[ A-Z]*PRIVATE KEY-----.*?-----END[ A-Z]*PRIVATE KEY-----", re.DOTALL)
_AUTH_HEADER_RE = re.compile(r"(?i)\b(authorization|proxy-authorization)\s*:\s*\S+(?:[ \t]+\S+)?")
_COOKIE_RE = re.compile(r"(?i)\b(set-cookie|cookie)\s*:\s*\S+")
_KV_SECRET_RE = re.compile(
    r"(?i)\b(api[-_]?key|apikey|access[-_]?token|refresh[-_]?token|auth[-_]?token"
    r"|client[-_]?secret|secret[-_]?key|token|secret|password|passwd|pwd|passphrase)"
    r"(\"?\s*[=:]\s*\"?)"
    r"([^\s\"',;&)}\]]{4,})")
_SIMPLE_SECRET_RES: Sequence["re.Pattern[str]"] = (
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
)


class Redactor:
    """Replaces credential-looking substrings before anything leaves the machine.

    Defence in depth, not a guarantee: it does not claim to be complete.
    """

    def __init__(self) -> None:
        self.count = 0

    def scrub(self, text: Optional[str]) -> str:
        if not text:
            return ""
        result = text
        result, hits = _PRIVATE_KEY_RE.subn(REDACTED, result)
        self.count += hits
        result, hits = _AUTH_HEADER_RE.subn(lambda m: "%s: %s" % (m.group(1), REDACTED), result)
        self.count += hits
        result, hits = _COOKIE_RE.subn(lambda m: "%s: %s" % (m.group(1), REDACTED), result)
        self.count += hits
        result, hits = _KV_SECRET_RE.subn(
            lambda m: "%s%s%s" % (m.group(1), m.group(2), REDACTED), result)
        self.count += hits
        for pattern in _SIMPLE_SECRET_RES:
            result, hits = pattern.subn(REDACTED, result)
            self.count += hits
        return result

    @staticmethod
    def looks_like_credential_path(path: Optional[str]) -> bool:
        if not path:
            return False
        lowered = str(path).replace("\\", "/").lower()
        base = lowered.rsplit("/", 1)[-1]
        for hint in CREDENTIAL_PATH_HINTS:
            if hint.startswith("."):
                if base == hint or base.endswith(hint):
                    return True
            elif hint in base:
                return True
        return False


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def deep_merge(base: Dict[str, Any], override: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(config_path: Optional[Path]) -> Dict[str, Any]:
    """Defaults <- config.json <- environment overrides."""
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    if config_path and config_path.is_file():
        try:
            with open(config_path, "r", encoding="utf-8") as handle:
                stored = json.load(handle)
            if isinstance(stored, dict):
                config = deep_merge(config, stored)
            else:
                warn("%s does not contain a JSON object; using defaults." % config_path)
        except ValueError as exc:
            warn("%s is not valid JSON (%s); using defaults." % (config_path, exc))
        except OSError as exc:
            warn("cannot read %s (%s); using defaults." % (config_path, exc))

    if os.environ.get("HANDOFF_LLM_BASE_URL"):
        config["llm"]["base_url"] = os.environ["HANDOFF_LLM_BASE_URL"]
        config["llm"]["enabled"] = True
    if os.environ.get("HANDOFF_LLM_MODEL"):
        config["llm"]["model"] = os.environ["HANDOFF_LLM_MODEL"]
    if os.environ.get("HANDOFF_LLM_TIMEOUT"):
        try:
            config["llm"]["timeout_seconds"] = int(os.environ["HANDOFF_LLM_TIMEOUT"])
        except ValueError:
            warn("HANDOFF_LLM_TIMEOUT is not an integer; ignoring it.")
    return config


def llm_is_configured(config: Dict[str, Any]) -> bool:
    """True only when an endpoint is actually available to call.

    Having base_url and model is what makes the AI step possible; "enabled" is
    just an explicit opt-out. The AI step is optional, so with no integration
    nothing here ever touches the network - not even to check.
    """
    llm = config.get("llm") or {}
    if llm.get("enabled") is False:
        return False
    return bool(llm.get("base_url")) and bool(llm.get("model"))


def llm_hint(config: Dict[str, Any]) -> str:
    """What to change to turn the AI step on, for a human reading `doctor`."""
    llm = config.get("llm") or {}
    if llm.get("enabled") is False:
        return 'set "enabled": true in .handoff/config.json'
    missing = [name for name in ("base_url", "model") if not llm.get(name)]
    if len(missing) == 2:
        return "set base_url + model in .handoff/config.json, or export HANDOFF_LLM_BASE_URL"
    if missing:
        return "set %s in .handoff/config.json" % missing[0]
    return "an endpoint is already configured"


def llm_skip_reason(config: Dict[str, Any]) -> str:
    """Why the AI step is not going to run, phrased as a complete reason."""
    llm = config.get("llm") or {}
    if llm.get("enabled") is False:
        return 'the AI step is switched off by "enabled": false in .handoff/config.json'
    return "no LLM integration configured - %s" % llm_hint(config)


def evidence_limit(config: Dict[str, Any], key: str) -> int:
    section = config.get("evidence") or {}
    value = section.get(key, DEFAULT_CONFIG["evidence"][key])
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return int(DEFAULT_CONFIG["evidence"][key])


# --------------------------------------------------------------------------
# Git
# --------------------------------------------------------------------------


@dataclass
class GitState:
    root: str = ""
    branch: str = ""
    head: str = ""
    head_subject: str = ""
    detached: bool = False
    status_porcelain: str = ""
    diff_stat: str = ""
    diff_name_status: str = ""
    staged_stat: str = ""
    staged_name_status: str = ""
    recent_commits: str = ""
    modified: List[str] = field(default_factory=list)
    added: List[str] = field(default_factory=list)
    deleted: List[str] = field(default_factory=list)
    renamed: List[str] = field(default_factory=list)
    untracked: List[str] = field(default_factory=list)
    conflicted: List[str] = field(default_factory=list)

    @property
    def is_dirty(self) -> bool:
        return bool(self.status_porcelain.strip())

    def changed_paths(self) -> List[str]:
        seen: List[str] = []
        for group in (self.modified, self.added, self.renamed, self.deleted,
                      self.conflicted, self.untracked):
            for item in group:
                if item not in seen:
                    seen.append(item)
        return seen


class GitInspector:
    """Deterministic Git collection. Never mutates the repository."""

    def __init__(self, cwd: Optional[Path] = None) -> None:
        self.cwd = Path(cwd) if cwd else Path.cwd()

    def run(self, args: Sequence[str], check: bool = False) -> Tuple[int, str]:
        try:
            completed = subprocess.run(
                ["git"] + list(args),
                cwd=str(self.cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError:
            raise HandoffError("git executable not found in PATH.", EXIT_GIT)
        except OSError as exc:
            raise HandoffError("failed to run git: %s" % exc, EXIT_GIT)
        if check and completed.returncode != 0:
            raise HandoffError(
                "git %s failed: %s" % (" ".join(args), (completed.stderr or "").strip()),
                EXIT_GIT,
            )
        return completed.returncode, (completed.stdout or "")

    def find_root(self) -> Path:
        code, out = self.run(["rev-parse", "--show-toplevel"])
        if code != 0 or not out.strip():
            raise HandoffError(
                "not inside a Git repository (%s).\n"
                "This tool needs a Git checkout. Either run it from your project,\n"
                "point it at one with --repo PATH, or run `git init` there first."
                % self.cwd,
                EXIT_GIT,
            )
        return Path(out.strip())

    def collect(self) -> GitState:
        state = GitState()
        state.root = str(self.find_root())

        _, branch = self.run(["branch", "--show-current"])
        state.branch = branch.strip()
        if not state.branch:
            state.detached = True
            _, described = self.run(["describe", "--all", "--always", "HEAD"])
            state.branch = "(detached: %s)" % described.strip() if described.strip() else "(detached HEAD)"

        code, head = self.run(["rev-parse", "HEAD"])
        state.head = head.strip() if code == 0 else "(no commits yet)"
        _, subject = self.run(["log", "-1", "--pretty=%s"])
        state.head_subject = subject.strip()

        _, state.status_porcelain = self.run(["status", "--porcelain=v1"])
        _, state.diff_stat = self.run(["diff", "--stat"])
        _, state.diff_name_status = self.run(["diff", "--name-status"])
        _, state.staged_stat = self.run(["diff", "--cached", "--stat"])
        _, state.staged_name_status = self.run(["diff", "--cached", "--name-status"])
        _, state.recent_commits = self.run(["log", "-10", "--oneline", "--decorate"])

        self._classify(state)
        return state

    @staticmethod
    def _classify(state: GitState) -> None:
        for line in state.status_porcelain.splitlines():
            if len(line) < 4:
                continue
            code = line[:2]
            path = line[3:].strip()
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            path = path.strip('"')
            if code == "??":
                state.untracked.append(path)
            elif "U" in code or code in ("AA", "DD"):
                state.conflicted.append(path)
            elif "R" in code:
                state.renamed.append(path)
            elif "D" in code:
                state.deleted.append(path)
            elif "A" in code:
                state.added.append(path)
            else:
                state.modified.append(path)


def render_git_state(state: GitState, generated_at: str) -> str:
    def section(title: str, body: str) -> str:
        body = (body or "").rstrip()
        return "=== %s ===\n\n%s\n\n" % (title, body if body else "(none)")

    parts = [
        "Generated: %s\n" % generated_at,
        "Repository: %s\n" % state.root,
        "Branch: %s\n" % state.branch,
        "HEAD: %s\n" % state.head,
    ]
    if state.head_subject:
        parts.append("HEAD subject: %s\n" % state.head_subject)
    parts.append("\n")
    parts.append(section("STATUS (git status --porcelain=v1)", state.status_porcelain))
    parts.append(section("DIFF STAT (unstaged)", state.diff_stat))
    parts.append(section("CHANGED FILES (git diff --name-status)", state.diff_name_status))
    parts.append(section("STAGED STAT (git diff --cached --stat)", state.staged_stat))
    parts.append(section("STAGED FILES (git diff --cached --name-status)", state.staged_name_status))
    parts.append(section("RECENT COMMITS (git log -10 --oneline --decorate)", state.recent_commits))
    return "".join(parts)


# --------------------------------------------------------------------------
# Session / event models
# --------------------------------------------------------------------------


@dataclass
class SessionInfo:
    agent: str
    session_id: str
    path: Path
    mtime: float
    size: int
    cwd: Optional[str] = None
    # "confirmed": cwd inside the repo. "unknown": no cwd evidence found.
    # "foreign": cwd points at a different checkout.
    match: str = "unknown"
    note: str = ""

    @property
    def mtime_iso(self) -> str:
        # A snapshot reused by `consolidate` can point at a transcript that is
        # gone, leaving mtime at 0; that is not an error worth crashing on.
        if not self.mtime:
            return "unknown"
        try:
            return datetime.fromtimestamp(self.mtime).astimezone().replace(microsecond=0).isoformat()
        except (OverflowError, OSError, ValueError):
            return "unknown"

    def describe(self) -> str:
        return "%s\n  modified %s\n  %s" % (
            self.session_id or self.path.name, self.mtime_iso, human_size(self.size))


@dataclass
class NormalizedEvent:
    timestamp: Optional[str] = None
    agent: str = ""
    session_id: str = ""
    kind: str = KIND_OTHER
    role: Optional[str] = None
    text: str = ""
    tool_name: Optional[str] = None
    file_paths: List[str] = field(default_factory=list)
    is_error: bool = False
    raw_type: str = ""
    mutating: bool = False

    def header(self) -> str:
        label = {
            KIND_USER: "USER",
            KIND_ASSISTANT: "ASSISTANT",
            KIND_TOOL_CALL: "TOOL CALL",
            KIND_TOOL_RESULT: "TOOL RESULT",
            KIND_TOOL_ERROR: "TOOL ERROR",
            KIND_COMPACTION: "COMPACTION SUMMARY",
            KIND_METADATA: "SESSION METADATA",
        }.get(self.kind, self.kind.upper())
        if self.tool_name:
            label = "%s (%s)" % (label, self.tool_name)
        if self.timestamp:
            label = "%s - %s" % (label, self.timestamp)
        return label


_WRAPPER_BLOCK_RE = re.compile(
    r"<([a-z][a-z0-9_\-]{2,40})(?:\s[^>]*)?>.*?</\1>", re.IGNORECASE | re.DOTALL)
_INJECTED_PROBE_CHARS = 20000
_INJECTED_RESIDUE_CHARS = 40


def is_injected_context(text: Optional[str]) -> bool:
    """True when a user-role message is really a harness-injected context block.

    Both CLIs inject machine-written blocks as user-role messages:
    <recommended_plugins>, <environment_context>, <system-reminder>,
    <user_instructions>, sometimes several concatenated in one message. If
    removing every complete XML element leaves almost nothing, nobody typed it,
    so it must not be mistaken for the user's actual request.
    """
    stripped = (text or "").strip()
    if not stripped.startswith("<"):
        return False
    residue = _WRAPPER_BLOCK_RE.sub("", stripped[:_INJECTED_PROBE_CHARS])
    return len(residue.strip()) < _INJECTED_RESIDUE_CHARS


def is_mutating_tool(name: Optional[str]) -> bool:
    if not name:
        return False
    lowered = str(name).lower()
    if lowered in MUTATING_TOOL_NAMES:
        return True
    return any(keyword in lowered for keyword in MUTATION_KEYWORDS)


def looks_like_mutating_shell_text(text: Optional[str]) -> bool:
    """Best-effort: does a shell command / free-form tool input write to disk?

    Used for tools whose own name gives no hint (Codex's "exec", Gemini's
    "run_shell_command") - the mutation signal has to come from the command
    text itself instead of the tool name.
    """
    lowered = (text or "")[:2000].lower()
    return any(token in lowered for token in
               ("apply_patch", "set-content", "out-file", ">>", "add-content"))


def looks_like_failed_output(text: Optional[str]) -> bool:
    """Best-effort: does a tool result/output look like a failure?

    Shared by adapters whose transcripts don't carry an explicit
    success/failure flag on the result (Codex's function_call_output,
    Gemini's toolCalls[].result) and so must be judged from the text itself.
    """
    head = (text or "")[:1500]
    if re.search(r'"exit_code"\s*:\s*[1-9]', head):
        return True
    if re.search(r"(?im)^\s*(exit code|exit status)\s*[:=]?\s*[1-9]", head):
        return True
    return bool(re.search(r"(?i)\b(traceback \(most recent call last\)|fatal error|command failed)\b", head))


# --------------------------------------------------------------------------
# Adapters
# --------------------------------------------------------------------------


class BaseAdapter:
    agent = ""
    label = ""

    def __init__(self, repo_root: Path, home: Optional[Path] = None) -> None:
        self.repo_root = Path(repo_root)
        self.home = Path(home) if home else Path.home()

    # -- discovery ------------------------------------------------------
    def root_dir(self) -> Path:
        raise NotImplementedError

    def available(self) -> bool:
        return self.root_dir().is_dir()

    def candidate_files(self) -> List[Path]:
        raise NotImplementedError

    def probe(self, path: Path) -> Tuple[Optional[str], Optional[str]]:
        """Cheap head-scan returning (session_id, cwd)."""
        raise NotImplementedError

    def discover(self, probe_limit: int = 40) -> List[SessionInfo]:
        """Sessions ordered by real mtime, newest first, tagged with repo match."""
        files = self.candidate_files()
        entries: List[Tuple[float, int, Path]] = []
        for path in files:
            try:
                stat = path.stat()
            except OSError:
                continue
            entries.append((stat.st_mtime, stat.st_size, path))
        entries.sort(key=lambda item: item[0], reverse=True)

        sessions: List[SessionInfo] = []
        root = normalize_path(str(self.repo_root))
        for index, (mtime, size, path) in enumerate(entries):
            session_id: Optional[str] = None
            cwd: Optional[str] = None
            if index < probe_limit:
                try:
                    session_id, cwd = self.probe(path)
                except HandoffError:
                    continue
                except OSError as exc:
                    log_verbose("probe failed for %s: %s" % (path, exc))
            info = SessionInfo(
                agent=self.agent,
                session_id=session_id or self.session_id_from_path(path),
                path=path,
                mtime=mtime,
                size=size,
                cwd=cwd,
            )
            if cwd:
                if path_is_within(cwd, root) or path_is_within(root, normalize_path(cwd)):
                    info.match = "confirmed"
                    info.note = "cwd recorded in session"
                else:
                    info.match = "foreign"
                    info.note = "session cwd is %s" % cwd
            else:
                info.match = "unknown"
                info.note = "no cwd found in session head" if index < probe_limit else "not probed"
                if self.path_hint_matches(path):
                    info.match = "hinted"
                    info.note = "session directory matches repository path"
            sessions.append(info)
        return sessions

    def path_hint_matches(self, path: Path) -> bool:
        return False

    @staticmethod
    def session_id_from_path(path: Path) -> str:
        return path.stem

    def select(
        self,
        session_id: Optional[str] = None,
        session_file: Optional[str] = None,
    ) -> SessionInfo:
        """Explicit selection wins; otherwise the newest session tied to this repo."""
        if session_file:
            path = Path(session_file).expanduser()
            if not path.is_file():
                raise HandoffError("session file not found: %s" % path, EXIT_NO_SESSION)
            try:
                stat = path.stat()
            except OSError as exc:
                raise HandoffError("cannot stat %s: %s" % (path, exc), EXIT_NO_SESSION)
            probed_id, cwd = self.probe(path)
            info = SessionInfo(
                agent=self.agent,
                session_id=probed_id or self.session_id_from_path(path),
                path=path, mtime=stat.st_mtime, size=stat.st_size, cwd=cwd,
                match="explicit", note="selected with --session-file",
            )
            return info

        sessions = self.discover()
        if not sessions:
            raise HandoffError(
                "no %s sessions found under %s" % (self.label, self.root_dir()),
                EXIT_NO_SESSION,
            )

        if session_id:
            wanted = session_id.lower()
            for info in sessions:
                if (info.session_id or "").lower().startswith(wanted) or wanted in info.path.name.lower():
                    info.note = (info.note + "; " if info.note else "") + "selected with --session"
                    return info
            raise HandoffError(
                "%s session '%s' not found. Try `handoff.py doctor` to list what exists."
                % (self.label, session_id),
                EXIT_NO_SESSION,
            )

        for wanted_match in ("confirmed", "hinted"):
            for info in sessions:
                if info.match == wanted_match:
                    return info

        foreign = [s for s in sessions if s.match == "foreign"]
        if foreign and all(s.match == "foreign" for s in sessions):
            raise HandoffError(
                "found %d %s session(s) but all belong to other checkouts "
                "(most recent: %s).\nUse --session or --session-file to choose explicitly."
                % (len(foreign), self.label, foreign[0].cwd),
                EXIT_NO_SESSION,
            )
        raise HandoffError(
            "no %s session could be tied to %s.\n"
            "Use --session or --session-file to choose one explicitly."
            % (self.label, self.repo_root),
            EXIT_NO_SESSION,
        )

    # -- parsing --------------------------------------------------------
    def iter_events(self, path: Path) -> Iterator[Tuple[Optional[NormalizedEvent], bool]]:
        raise NotImplementedError


class ClaudeAdapter(BaseAdapter):
    """Reads ~/.claude/projects/<slug>/<session-uuid>.jsonl.

    Observed record shape (Claude Code 2.x):
      {"type":"user"|"assistant", "message":{"role":..,"content":[blocks]},
       "cwd":..., "gitBranch":..., "sessionId":..., "timestamp":"...Z", "uuid":...}
    Content blocks: text | thinking | tool_use | tool_result | image.
    Errors surface as tool_result blocks with "is_error": true.
    Bookkeeping records (attachment, file-history-*, ai-title, mode, ...) are ignored.
    """

    agent = AGENT_CLAUDE
    label = "Claude"

    CONVERSATION_TYPES = {"user", "assistant"}
    IGNORED_TYPES = {
        "attachment", "file-history-snapshot", "file-history-delta", "bridge-session",
        "atis-latch", "last-prompt", "ai-title", "queue-operation", "mode",
    }

    def root_dir(self) -> Path:
        return self.home / ".claude" / "projects"

    @staticmethod
    def slug_for(path: str) -> str:
        """Claude derives a project directory name by replacing non-alphanumerics with '-'."""
        return re.sub(r"[^A-Za-z0-9]", "-", str(path))

    def expected_slugs(self) -> List[str]:
        raw = str(self.repo_root)
        variants = {raw, raw.replace("/", os.sep), str(Path(raw))}
        slugs = []
        for variant in variants:
            slug = self.slug_for(variant)
            if slug not in slugs:
                slugs.append(slug)
        return slugs

    def path_hint_matches(self, path: Path) -> bool:
        parent = path.parent.name.lower()
        return any(parent == slug.lower() for slug in self.expected_slugs())

    def candidate_files(self) -> List[Path]:
        root = self.root_dir()
        if not root.is_dir():
            return []
        files: List[Path] = []
        try:
            project_dirs = [entry for entry in root.iterdir() if entry.is_dir()]
        except OSError:
            return []
        for project_dir in project_dirs:
            try:
                # Top-level only: nested <session>/subagents/*.jsonl belong to a parent session.
                files.extend(sorted(project_dir.glob("*.jsonl")))
            except OSError:
                continue
        return files

    def probe(self, path: Path, max_lines: int = 200, max_bytes: int = 512 * 1024) -> Tuple[Optional[str], Optional[str]]:
        session_id: Optional[str] = None
        cwd: Optional[str] = None
        read = 0
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for index, line in enumerate(handle):
                    read += len(line)
                    if index >= max_lines or read >= max_bytes:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    session_id = session_id or record.get("sessionId")
                    candidate = record.get("cwd")
                    if not candidate:
                        attachment = record.get("attachment")
                        if isinstance(attachment, dict):
                            snapshot = attachment.get("snapshot")
                            if isinstance(snapshot, dict):
                                candidate = snapshot.get("workingDirectory")
                    if candidate and not cwd:
                        cwd = str(candidate)
                    if session_id and cwd:
                        break
        except OSError as exc:
            log_verbose("cannot probe %s: %s" % (path, exc))
        return session_id, cwd

    def iter_events(self, path: Path) -> Iterator[Tuple[Optional[NormalizedEvent], bool]]:
        for record, ok in iter_jsonl(path):
            if not ok or record is None:
                yield None, False
                continue
            for event in self._normalize(record):
                yield event, True

    def _normalize(self, record: Dict[str, Any]) -> List[NormalizedEvent]:
        raw_type = str(record.get("type") or "")
        if raw_type in self.IGNORED_TYPES:
            return []

        session_id = str(record.get("sessionId") or "")
        timestamp = ts_to_iso(record.get("timestamp"))
        base = dict(agent=self.agent, session_id=session_id, timestamp=timestamp, raw_type=raw_type)

        # Compaction: documented as a system/compact_boundary record, a
        # dedicated "summary" record, or a user message flagged isCompactSummary.
        if raw_type == "summary":
            text = record.get("summary") or record.get("text") or ""
            return [NormalizedEvent(kind=KIND_COMPACTION, text=str(text), **base)]
        if raw_type == "system" and record.get("subtype") == "compact_boundary":
            meta = record.get("compactMetadata") or {}
            detail = json.dumps(meta, ensure_ascii=False) if meta else "compaction boundary"
            return [NormalizedEvent(kind=KIND_COMPACTION, text=detail, **base)]
        if raw_type == "system":
            return []

        if raw_type not in self.CONVERSATION_TYPES:
            return []

        message = record.get("message")
        if not isinstance(message, dict):
            return []
        role = message.get("role") or raw_type
        content = message.get("content")
        is_compact_summary = bool(record.get("isCompactSummary"))

        events: List[NormalizedEvent] = []
        if isinstance(content, str):
            kind = KIND_COMPACTION if is_compact_summary else (
                KIND_USER if role == "user" else KIND_ASSISTANT)
            if content.strip():
                events.append(NormalizedEvent(kind=kind, role=role, text=content, **base))
            return events

        if not isinstance(content, list):
            return []

        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                text = str(block.get("text") or "")
                if not text.strip():
                    continue
                if role == "user" and not is_compact_summary and is_injected_context(text):
                    continue  # <system-reminder> and friends: injected, not typed
                kind = KIND_COMPACTION if is_compact_summary else (
                    KIND_USER if role == "user" else KIND_ASSISTANT)
                events.append(NormalizedEvent(kind=kind, role=role, text=text, **base))
            elif block_type == "tool_use":
                name = str(block.get("name") or "")
                tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                events.append(NormalizedEvent(
                    kind=KIND_TOOL_CALL, role=role, tool_name=name,
                    text=self._describe_tool_input(name, tool_input),
                    file_paths=self._paths_from_input(tool_input),
                    mutating=is_mutating_tool(name), **base))
            elif block_type == "tool_result":
                text = self._flatten_result(block.get("content"))
                is_error = bool(block.get("is_error"))
                events.append(NormalizedEvent(
                    kind=KIND_TOOL_ERROR if is_error else KIND_TOOL_RESULT,
                    role=role, text=text, is_error=is_error, **base))
            # "thinking" carries opaque signatures and "image" carries base64: both skipped.
        return events

    @staticmethod
    def _describe_tool_input(name: str, tool_input: Dict[str, Any]) -> str:
        if not tool_input:
            return name
        interesting = ("command", "file_path", "path", "pattern", "notebook_path",
                       "url", "query", "description", "prompt", "old_string", "new_string")
        parts: List[str] = []
        for key in interesting:
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                parts.append("%s: %s" % (key, value.strip()))
        if not parts:
            try:
                return json.dumps(tool_input, ensure_ascii=False)[:800]
            except (TypeError, ValueError):
                return name
        return "\n".join(parts)

    @staticmethod
    def _paths_from_input(tool_input: Dict[str, Any]) -> List[str]:
        paths: List[str] = []
        for key in ("file_path", "path", "notebook_path", "filePath"):
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                paths.append(value.strip())
        edits = tool_input.get("edits")
        if isinstance(edits, list):
            for edit in edits[:20]:
                if isinstance(edit, dict):
                    value = edit.get("file_path") or edit.get("path")
                    if isinstance(value, str):
                        paths.append(value)
        return paths

    @staticmethod
    def _flatten_result(content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: List[str] = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(str(block.get("text") or ""))
                    elif block.get("type") == "image":
                        parts.append("[image omitted]")
            return "\n".join(part for part in parts if part)
        if isinstance(content, dict):
            if content.get("type") == "text":
                return str(content.get("text") or "")
            try:
                return json.dumps(content, ensure_ascii=False)[:4000]
            except (TypeError, ValueError):
                return ""
        return str(content)


class CodexAdapter(BaseAdapter):
    """Reads ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl.

    Observed record shape (Codex CLI 0.15x):
      {"timestamp":..., "ordinal":N, "type":..., "payload":{...}}
    Types: session_meta | turn_context | response_item | event_msg
           | token_usage_record | world_state | compacted.
    Conversation lives in response_item payloads (message / reasoning /
    custom_tool_call / custom_tool_call_output / function_call*).
    FileChange items only appear in event_msg/item_completed, so those are
    taken from there while messages are taken from response_item to avoid
    counting the same turn twice.
    """

    agent = AGENT_CODEX
    label = "Codex"

    def __init__(self, repo_root: Path, home: Optional[Path] = None) -> None:
        super().__init__(repo_root, home)
        self._seen_user_texts: set = set()

    def root_dir(self) -> Path:
        return self.home / ".codex" / "sessions"

    def candidate_files(self) -> List[Path]:
        root = self.root_dir()
        if not root.is_dir():
            return []
        try:
            files = list(root.glob("**/rollout-*.jsonl"))
            if not files:
                files = list(root.glob("**/*.jsonl"))
        except OSError:
            return []
        return files

    @staticmethod
    def session_id_from_path(path: Path) -> str:
        match = re.search(
            r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
            path.name)
        return match.group(1) if match else path.stem

    def probe(self, path: Path, max_lines: int = 60, max_bytes: int = 512 * 1024) -> Tuple[Optional[str], Optional[str]]:
        session_id: Optional[str] = None
        cwd: Optional[str] = None
        read = 0
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for index, line in enumerate(handle):
                    read += len(line)
                    if index >= max_lines or read >= max_bytes:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    payload = record.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    session_id = session_id or payload.get("session_id") or payload.get("id")
                    if not cwd and payload.get("cwd"):
                        cwd = str(payload["cwd"])
                    if not cwd:
                        roots = payload.get("workspace_roots")
                        if isinstance(roots, list) and roots and isinstance(roots[0], str):
                            cwd = roots[0]
                    if session_id and cwd:
                        break
        except OSError as exc:
            log_verbose("cannot probe %s: %s" % (path, exc))
        if session_id and not re.match(r"^[0-9a-fA-F-]{8,}$", str(session_id)):
            session_id = None
        return (str(session_id) if session_id else None), cwd

    def iter_events(self, path: Path) -> Iterator[Tuple[Optional[NormalizedEvent], bool]]:
        session_id = ""
        self._seen_user_texts = set()
        for record, ok in iter_jsonl(path):
            if not ok or record is None:
                yield None, False
                continue
            payload = record.get("payload")
            if isinstance(payload, dict) and payload.get("session_id"):
                session_id = session_id or str(payload["session_id"])
            for event in self._normalize(record, session_id):
                yield event, True

    def _normalize(self, record: Dict[str, Any], session_id: str) -> List[NormalizedEvent]:
        raw_type = str(record.get("type") or "")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return []
        timestamp = ts_to_iso(record.get("timestamp"))
        base = dict(agent=self.agent, session_id=session_id, timestamp=timestamp)

        if raw_type == "session_meta":
            details = []
            for key in ("session_id", "cwd", "originator", "cli_version", "model_provider"):
                if payload.get(key):
                    details.append("%s: %s" % (key, payload[key]))
            return [NormalizedEvent(kind=KIND_METADATA, text="\n".join(details),
                                    raw_type=raw_type, **base)]

        if raw_type == "compacted":
            history = payload.get("replacement_history")
            summary = payload.get("message") or ""
            if not summary and isinstance(history, list):
                summary = "context compaction: history replaced with %d retained item(s)" % len(history)
            return [NormalizedEvent(kind=KIND_COMPACTION, text=str(summary),
                                    raw_type=raw_type, **base)]

        if raw_type == "response_item":
            return self._normalize_response_item(payload, base, raw_type)

        if raw_type == "event_msg":
            return self._normalize_event_msg(payload, base, raw_type)

        return []

    def _normalize_response_item(
        self, payload: Dict[str, Any], base: Dict[str, Any], raw_type: str
    ) -> List[NormalizedEvent]:
        payload_type = str(payload.get("type") or "")

        if payload_type == "message":
            role = str(payload.get("role") or "")
            if role == "developer":
                return []  # system prompt / skill boilerplate, not conversation
            text = self._flatten_content(payload.get("content"))
            if not text.strip():
                return []
            if role == "user":
                if is_injected_context(text):
                    return []
                digest = sha256_text(text.strip())
                if digest in self._seen_user_texts:
                    return []  # already emitted from an item_completed record
                self._seen_user_texts.add(digest)
            kind = KIND_USER if role == "user" else KIND_ASSISTANT
            return [NormalizedEvent(kind=kind, role=role, text=text,
                                    raw_type="%s/%s" % (raw_type, payload_type), **base)]

        if payload_type in ("custom_tool_call", "function_call", "local_shell_call"):
            name = str(payload.get("name") or payload_type)
            raw_input = payload.get("input")
            if raw_input is None:
                raw_input = payload.get("arguments")
            text = raw_input if isinstance(raw_input, str) else json.dumps(
                raw_input, ensure_ascii=False, default=str)
            return [NormalizedEvent(
                kind=KIND_TOOL_CALL, tool_name=name, text=text,
                mutating=is_mutating_tool(name) or self._input_mutates(text),
                raw_type="%s/%s" % (raw_type, payload_type), **base)]

        if payload_type in ("custom_tool_call_output", "function_call_output", "local_shell_call_output"):
            text = self._flatten_content(payload.get("output"))
            is_error = self._looks_like_failure(text)
            return [NormalizedEvent(
                kind=KIND_TOOL_ERROR if is_error else KIND_TOOL_RESULT,
                text=text, is_error=is_error,
                raw_type="%s/%s" % (raw_type, payload_type), **base)]

        # "reasoning" holds encrypted content: nothing usable, skipped.
        return []

    def _normalize_event_msg(
        self, payload: Dict[str, Any], base: Dict[str, Any], raw_type: str
    ) -> List[NormalizedEvent]:
        payload_type = str(payload.get("type") or "")
        if payload_type != "item_completed":
            return []
        item = payload.get("item")
        if not isinstance(item, dict):
            return []
        item_type = str(item.get("type") or "")

        if item_type == "FileChange":
            changes = item.get("changes")
            if not isinstance(changes, dict):
                return []
            paths = list(changes.keys())
            summary = []
            for path, detail in list(changes.items())[:40]:
                change_kind = detail.get("type") if isinstance(detail, dict) else "change"
                summary.append("%s: %s" % (change_kind, path))
            return [NormalizedEvent(
                kind=KIND_TOOL_CALL, tool_name="FileChange", text="\n".join(summary),
                file_paths=paths, mutating=True,
                raw_type="%s/item/FileChange" % raw_type, **base)]

        if item_type == "ContextCompaction":
            return [NormalizedEvent(kind=KIND_COMPACTION,
                                    text="context compaction performed by Codex",
                                    raw_type="%s/item/ContextCompaction" % raw_type, **base)]

        if item_type == "Error":
            text = self._flatten_content(item.get("message") or item.get("content"))
            return [NormalizedEvent(kind=KIND_TOOL_ERROR, text=text, is_error=True,
                                    raw_type="%s/item/Error" % raw_type, **base)]

        if item_type == "McpToolCall":
            name = "%s/%s" % (item.get("server") or "mcp", item.get("tool") or "?")
            title = item.get("title") or ""
            return [NormalizedEvent(kind=KIND_TOOL_CALL, tool_name=name, text=str(title),
                                    raw_type="%s/item/McpToolCall" % raw_type, **base)]

        if item_type == "UserMessage":
            # Normally a duplicate of response_item/message role=user, which is
            # richer. Kept as a fallback so user turns survive if a Codex build
            # ever stops writing that record; deduped by content hash.
            text = self._flatten_content(item.get("content"))
            if not text.strip() or is_injected_context(text):
                return []
            digest = sha256_text(text.strip())
            if digest in self._seen_user_texts:
                return []
            self._seen_user_texts.add(digest)
            return [NormalizedEvent(kind=KIND_USER, role="user", text=text,
                                    raw_type="%s/item/UserMessage" % raw_type, **base)]

        # AgentMessage duplicates response_item/message role=assistant: skipped there.
        return []

    @staticmethod
    def _input_mutates(text: str) -> bool:
        return looks_like_mutating_shell_text(text)

    @staticmethod
    def _looks_like_failure(text: str) -> bool:
        return looks_like_failed_output(text)

    @staticmethod
    def _flatten_content(content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: List[str] = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict):
                    block_type = str(block.get("type") or "")
                    if block_type in ("input_text", "output_text", "text", "Text"):
                        parts.append(str(block.get("text") or ""))
                    elif block_type in ("input_image", "image"):
                        parts.append("[image omitted]")
            return "\n".join(part for part in parts if part)
        if isinstance(content, dict):
            return CodexAdapter._flatten_content(content.get("content") or content.get("text"))
        return str(content)


_GEMINI_SESSION_ID_RE = re.compile(r"-([0-9a-fA-F]{8})\.jsonl?$")
_GEMINI_ACK_TEXT = "Got it. Thanks for the additional context!"
_GEMINI_SHELL_TOOL_NAMES = {"run_shell_command", "execute_command", "shell"}


class GeminiAdapter(BaseAdapter):
    """Reads ~/.gemini/tmp/<project-slug>/chats/session-*.jsonl.

    There is no complete real transcript to inspect: the Gemini CLI (npm
    package @google/gemini-cli) needs a Google account or API key to complete
    a turn, and neither was available in the environment this adapter was
    built in. What follows was cross-checked two ways instead:

    1. A real, on-disk (if incomplete - it errored out before any model reply)
       session produced by running the actual CLI headless, confirming the
       project-registry layout (~/.gemini/projects.json, .project_root marker
       files) and the header/plain-message record shapes.
    2. The unminified source the npm package ships with its own bundle
       (packages/core/dist/src/services/chatRecordingService.js and
       .../config/projectRegistry.js, @google/gemini-cli 0.59.0) - read
       directly for every record shape and merge rule below, including ones
       the incomplete probe session never exercised (tool calls, thoughts,
       compaction, $set/$rewindTo semantics).

    Record shapes:
      Line 1: {"sessionId":..., "projectHash":..., "startTime":...,
               "lastUpdated":..., "kind":"main"|"subagent", "directories"?}
      Message record (has a string "id"):
        {"id":..., "timestamp":..., "type":"user"|"gemini"|"info"|"error"
                                          |"warning",
         "content": <string, or a list of GenAI Part-like objects
                     (text / functionCall / functionResponse / thought /
                     inlineData / ...)>,
         # "gemini" messages only:
         "thoughts"?: [...], "tokens"?: {...}, "model"?: str,
         "toolCalls"?: [{"id","name","args","result"?,"displayName",...}]}
      {"$set": {...partial metadata...}} - a partial update. When it carries
        a "messages" array, that array *replaces* the entire message list
        built so far (used by periodic history resyncs) rather than adding
        to it.
      {"$rewindTo": "<messageId>"} - discards that message and everything
        recorded after it (used when a stream aborts mid-turn).

    This is a patch/snapshot log, not a plain append log: a message id can be
    superseded or invalidated by a later record. Producing the *current*
    state therefore needs one id->message map built by replaying every
    record in order (matching the CLI's own loadConversationRecord), not a
    flat per-line emission like the Claude/Codex adapters use. iter_events
    still reads the file one line at a time (never loads it whole), but
    withholds normalized events until end of file so the map reflects every
    $set/$rewindTo it has seen; Gemini session files are chat text plus
    capped tool output (MAX_TOOL_OUTPUT_SIZE = 50 KiB per call in the same
    source), not the 30-100 MiB scale Claude/Codex sessions reach.

    Compaction has no dedicated record type. A real compaction is recorded as
    a synthetic "user" message whose text is a <state_snapshot>...</state_snapshot>
    block, immediately followed by a fixed "gemini" acknowledgement reply
    ("Got it. Thanks for the additional context!") - both come straight from
    the compression prompt template in the same source file. Both are
    detected and handled here; the acknowledgement is dropped as noise and
    the snapshot becomes the compaction event.

    A tool result can appear in two different shapes depending on whether the
    message was ever touched by a history resync: inline in a "gemini"
    message's own toolCalls[].result, or as a functionResponse Part inside a
    *separate* later "user"-role message (the Gemini API's own tool-response
    convention). Both are handled; the latter means a "user"-type record is
    not automatically a human message and must be checked for functionResponse
    parts first.

    Caveat: tool-call, thought, token and compaction handling is verified
    against the shipped source and a synthetic fixture built from it, not
    against a real completed round trip - only the header record and one
    plain user message were confirmed against genuine CLI output. Treat this
    adapter as less battle-tested than the Claude and Codex ones until it has
    been run against a real authenticated session.
    """

    agent = AGENT_GEMINI
    label = "Gemini"

    def root_dir(self) -> Path:
        return self.home / ".gemini" / "tmp"

    @staticmethod
    def slugify(text: str) -> str:
        """Mirrors ProjectRegistry.slugify() in projectRegistry.js exactly."""
        slug = re.sub(r"[^a-z0-9]", "-", text.lower())
        slug = re.sub(r"-+", "-", slug).strip("-")
        return slug or "project"

    def path_hint_matches(self, path: Path) -> bool:
        slug_dir_name = path.parent.parent.name.lower()
        expected = self.slugify(Path(self.repo_root).name)
        # A numeric collision suffix ("-1", "-2", ...) is part of their
        # scheme too; a prefix match still counts as a hint, just a weaker one.
        return slug_dir_name == expected or slug_dir_name.startswith(expected + "-")

    def candidate_files(self) -> List[Path]:
        root = self.root_dir()
        if not root.is_dir():
            return []
        files: List[Path] = []
        try:
            slug_dirs = [entry for entry in root.iterdir() if entry.is_dir()]
        except OSError:
            return []
        for slug_dir in slug_dirs:
            try:
                # Top-level only: nested <slug>/chats/<parentId>/*.jsonl are
                # subagent sessions belonging to a parent session.
                files.extend(sorted(slug_dir.glob("chats/*.jsonl")))
            except OSError:
                continue
        return files

    @staticmethod
    def session_id_from_path(path: Path) -> str:
        match = _GEMINI_SESSION_ID_RE.search(path.name)
        return match.group(1) if match else path.stem

    def probe(self, path: Path, max_lines: int = 5, max_bytes: int = 64 * 1024) -> Tuple[Optional[str], Optional[str]]:
        # cwd: the project-root marker sitting next to the chats/ directory
        # this session lives under - the same file the CLI itself trusts
        # (ProjectRegistry.verifySlugOwnership reads exactly this file).
        cwd: Optional[str] = None
        try:
            marker = path.parent.parent / ".project_root"
            cwd = marker.read_text(encoding="utf-8", errors="replace").strip() or None
        except OSError:
            pass

        session_id: Optional[str] = None
        read = 0
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for index, line in enumerate(handle):
                    read += len(line)
                    # Note: no handle.tell() here - Python disables tell() on a
                    # text file once next()/iteration has been used on it.
                    if index >= max_lines or read >= max_bytes:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(record, dict) and isinstance(record.get("sessionId"), str):
                        session_id = record["sessionId"]
                        break
        except OSError as exc:
            log_verbose("cannot probe %s: %s" % (path, exc))
        return session_id, cwd

    # -- content helpers --------------------------------------------------

    @staticmethod
    def _as_parts(content: Any) -> List[Dict[str, Any]]:
        """Mirrors ensurePartArray(): a bare string becomes one text part."""
        if content is None:
            return []
        if isinstance(content, str):
            return [{"text": content}] if content else []
        if isinstance(content, list):
            return [part if isinstance(part, dict) else {"text": str(part)} for part in content]
        if isinstance(content, dict):
            return [content]
        return [{"text": str(content)}]

    @staticmethod
    def _flatten_text(parts: Sequence[Dict[str, Any]]) -> str:
        """Visible text only: skips thoughts and non-text parts (getResponseText)."""
        chunks = [str(part.get("text") or "") for part in parts
                 if isinstance(part, dict) and part.get("text") and not part.get("thought")]
        return "\n".join(chunk for chunk in chunks if chunk)

    @staticmethod
    def _describe_args(args: Any) -> str:
        if not isinstance(args, dict) or not args:
            return json.dumps(args, ensure_ascii=False)[:800] if args else ""
        interesting = ("file_path", "path", "absolute_path", "old_path", "new_path",
                      "old_string", "new_string", "command", "pattern", "query", "url")
        parts = ["%s: %s" % (key, args[key]) for key in interesting
                if isinstance(args.get(key), str) and args[key].strip()]
        if parts:
            return "\n".join(parts)
        try:
            return json.dumps(args, ensure_ascii=False)[:800]
        except (TypeError, ValueError):
            return ""

    @staticmethod
    def _paths_from_args(args: Any) -> List[str]:
        if not isinstance(args, dict):
            return []
        paths = []
        for key in ("file_path", "path", "absolute_path", "old_path", "new_path"):
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                paths.append(value.strip())
        return paths

    @staticmethod
    def _flatten_tool_result(result: Any) -> str:
        if result is None:
            return ""
        if isinstance(result, str):
            return result
        if isinstance(result, list):
            return GeminiAdapter._flatten_text(GeminiAdapter._as_parts(result)) or \
                json.dumps(result, ensure_ascii=False, default=str)[:2000]
        if isinstance(result, dict):
            output = result.get("output")
            if isinstance(output, str):
                return output
            response = result.get("response")
            if isinstance(response, dict) and isinstance(response.get("output"), str):
                return response["output"]
            try:
                return json.dumps(result, ensure_ascii=False, default=str)[:2000]
            except (TypeError, ValueError):
                return str(result)
        return str(result)

    def _tool_mutates(self, name: str, args: Any) -> bool:
        if is_mutating_tool(name):
            return True
        if str(name).lower() in _GEMINI_SHELL_TOOL_NAMES:
            return looks_like_mutating_shell_text(self._describe_args(args))
        return False

    # -- parsing ------------------------------------------------------------

    def iter_events(self, path: Path) -> Iterator[Tuple[Optional[NormalizedEvent], bool]]:
        session_id = ""
        messages: "Dict[str, Dict[str, Any]]" = {}
        invalid_lines = 0

        for record, ok in iter_jsonl(path):
            if not ok or record is None:
                invalid_lines += 1
                continue

            if isinstance(record.get("$rewindTo"), str):
                rewind_id = record["$rewindTo"]
                if rewind_id in messages:
                    keep = []
                    found = False
                    for key in messages:
                        if key == rewind_id:
                            found = True
                        if not found:
                            keep.append(key)
                    messages = {key: messages[key] for key in keep}
                else:
                    messages = {}
                continue

            update = record.get("$set")
            if isinstance(update, dict):
                new_messages = update.get("messages")
                if isinstance(new_messages, list):
                    messages = {}
                    for item in new_messages:
                        if isinstance(item, dict) and isinstance(item.get("id"), str):
                            messages[item["id"]] = item
                continue

            if isinstance(record.get("sessionId"), str) and isinstance(record.get("projectHash"), str):
                session_id = session_id or record["sessionId"]
                inline_messages = record.get("messages")
                if isinstance(inline_messages, list):
                    for item in inline_messages:
                        if isinstance(item, dict) and isinstance(item.get("id"), str):
                            messages[item["id"]] = item
                continue

            if isinstance(record.get("id"), str):
                messages[record["id"]] = record
                continue
            # Anything else is a record shape not documented anywhere I could
            # verify: skipped rather than guessed at.

        for message in messages.values():
            for event in self._normalize_message(message, session_id):
                yield event, True

        for _ in range(invalid_lines):
            yield None, False

    def _normalize_message(self, record: Dict[str, Any], session_id: str) -> List[NormalizedEvent]:
        msg_type = str(record.get("type") or "")
        timestamp = ts_to_iso(record.get("timestamp"))
        base = dict(agent=self.agent, session_id=session_id, timestamp=timestamp, raw_type=msg_type)

        if msg_type in ("info", "warning"):
            return []

        if msg_type == "error":
            text = self._flatten_text(self._as_parts(record.get("content")))
            return [NormalizedEvent(kind=KIND_TOOL_ERROR, text=text, is_error=True, **base)]

        if msg_type == "user":
            return self._normalize_user_message(record, base)

        if msg_type == "gemini":
            return self._normalize_gemini_message(record, base)

        return []

    def _normalize_user_message(self, record: Dict[str, Any], base: Dict[str, Any]) -> List[NormalizedEvent]:
        parts = self._as_parts(record.get("content"))
        responses = [part for part in parts if isinstance(part.get("functionResponse"), dict)]
        if responses:
            events = []
            for part in responses:
                response = part["functionResponse"]
                text = self._flatten_tool_result(response.get("response"))
                events.append(NormalizedEvent(
                    kind=KIND_TOOL_ERROR if looks_like_failed_output(text) else KIND_TOOL_RESULT,
                    tool_name=response.get("name"), text=text,
                    is_error=looks_like_failed_output(text), **base))
            return events

        text = self._flatten_text(parts)
        trimmed = text.strip()
        # Checked before the ignored/injected filters below: a <state_snapshot>
        # is real compaction evidence, even though it is exactly the kind of
        # "just one XML wrapper" text is_injected_context() would otherwise
        # treat as harness noise and discard.
        if trimmed.startswith("<state_snapshot>") or "<state_snapshot>" in trimmed[:64]:
            return [NormalizedEvent(kind=KIND_COMPACTION, text=text, **base)]
        if (not trimmed or trimmed.startswith("/") or trimmed.startswith("?")
                or trimmed.startswith("<session_context>") or trimmed.startswith("<hook_context>")
                or is_injected_context(text)):
            return []  # matches isIgnoredUserContent(), plus the generic safety net
        return [NormalizedEvent(kind=KIND_USER, role="user", text=text, **base)]

    def _normalize_gemini_message(self, record: Dict[str, Any], base: Dict[str, Any]) -> List[NormalizedEvent]:
        events: List[NormalizedEvent] = []
        parts = self._as_parts(record.get("content"))

        text = self._flatten_text(parts)
        if text.strip() and text.strip() != _GEMINI_ACK_TEXT:
            events.append(NormalizedEvent(kind=KIND_ASSISTANT, role="gemini", text=text, **base))

        for part in parts:
            call = part.get("functionCall")
            if isinstance(call, dict):
                name = str(call.get("name") or "")
                args = call.get("args")
                events.append(NormalizedEvent(
                    kind=KIND_TOOL_CALL, tool_name=name, text=self._describe_args(args),
                    file_paths=self._paths_from_args(args), mutating=self._tool_mutates(name, args),
                    **base))

        for call in record.get("toolCalls") or []:
            if not isinstance(call, dict):
                continue
            name = str(call.get("name") or "")
            args = call.get("args")
            events.append(NormalizedEvent(
                kind=KIND_TOOL_CALL, tool_name=name, text=self._describe_args(args),
                file_paths=self._paths_from_args(args), mutating=self._tool_mutates(name, args),
                **base))
            if "result" in call and call["result"] is not None:
                result_text = self._flatten_tool_result(call["result"])
                events.append(NormalizedEvent(
                    kind=KIND_TOOL_ERROR if looks_like_failed_output(result_text) else KIND_TOOL_RESULT,
                    tool_name=name, text=result_text, is_error=looks_like_failed_output(result_text), **base))

        # "thoughts" holds free-form reasoning: opaque and skipped, matching
        # Claude's "thinking" blocks and Codex's "reasoning" records.
        return events


def get_adapter(agent: str, repo_root: Path, home: Optional[Path] = None) -> BaseAdapter:
    if agent == AGENT_CLAUDE:
        return ClaudeAdapter(repo_root, home)
    if agent == AGENT_CODEX:
        return CodexAdapter(repo_root, home)
    if agent == AGENT_GEMINI:
        return GeminiAdapter(repo_root, home)
    raise HandoffError("unknown agent '%s' (expected: %s)" % (agent, ", ".join(AGENTS)), EXIT_USAGE)


# --------------------------------------------------------------------------
# Evidence pack
# --------------------------------------------------------------------------


@dataclass
class EvidencePack:
    session: SessionInfo
    first_events: List[NormalizedEvent] = field(default_factory=list)
    last_events: List[NormalizedEvent] = field(default_factory=list)
    first_user_messages: List[NormalizedEvent] = field(default_factory=list)
    last_user_messages: List[NormalizedEvent] = field(default_factory=list)
    errors: List[NormalizedEvent] = field(default_factory=list)
    mutating_tools: List[NormalizedEvent] = field(default_factory=list)
    compactions: List[NormalizedEvent] = field(default_factory=list)
    touched_files: "Counter[str]" = field(default_factory=Counter)
    tool_usage: "Counter[str]" = field(default_factory=Counter)
    kind_counts: "Counter[str]" = field(default_factory=Counter)
    total_events: int = 0
    invalid_lines: int = 0
    redactions: int = 0
    session_cwd: Optional[str] = None

    def selected_count(self) -> int:
        """Distinct events kept, not the sum of the buckets (they overlap)."""
        seen = set()
        for bucket in (self.first_events, self.last_events, self.errors,
                       self.mutating_tools, self.compactions,
                       self.first_user_messages, self.last_user_messages):
            for event in bucket:
                seen.add(id(event))
        return len(seen)


class EvidenceBuilder:
    """One streaming pass over the transcript, bounded buffers only."""

    def __init__(self, config: Dict[str, Any], redactor: Optional[Redactor] = None) -> None:
        self.config = config
        self.redactor = redactor or Redactor()
        self.max_text = evidence_limit(config, "max_text_chars")
        self.max_tool_text = evidence_limit(config, "max_tool_text_chars")

    def build(self, adapter: BaseAdapter, session: SessionInfo) -> EvidencePack:
        first_limit = evidence_limit(self.config, "first_events")
        last_limit = evidence_limit(self.config, "last_events")
        user_limit = evidence_limit(self.config, "max_user_messages")

        pack = EvidencePack(session=session)
        last_events: deque = deque(maxlen=last_limit)
        errors: deque = deque(maxlen=evidence_limit(self.config, "max_errors"))
        mutating: deque = deque(maxlen=evidence_limit(self.config, "max_tool_events"))
        compactions: deque = deque(maxlen=evidence_limit(self.config, "max_compactions"))
        last_users: deque = deque(maxlen=max(1, user_limit // 2))

        for event, ok in adapter.iter_events(session.path):
            if not ok or event is None:
                pack.invalid_lines += 1
                continue
            pack.total_events += 1
            pack.kind_counts[event.kind] += 1

            if event.kind == KIND_METADATA:
                if not pack.session_cwd:
                    match = re.search(r"^cwd:\s*(.+)$", event.text or "", re.MULTILINE)
                    if match:
                        pack.session_cwd = match.group(1).strip()
                continue

            if event.tool_name:
                pack.tool_usage[event.tool_name] += 1
            for path in event.file_paths:
                pack.touched_files[path] += 1

            trimmed = self._trim(event)

            if event.kind == KIND_COMPACTION:
                compactions.append(trimmed)
                continue
            if event.is_error:
                errors.append(trimmed)
            if event.mutating and event.kind == KIND_TOOL_CALL:
                mutating.append(trimmed)

            if event.kind in (KIND_USER, KIND_ASSISTANT, KIND_TOOL_CALL,
                              KIND_TOOL_RESULT, KIND_TOOL_ERROR):
                if len(pack.first_events) < first_limit:
                    pack.first_events.append(trimmed)
                last_events.append(trimmed)

            if event.kind == KIND_USER:
                if len(pack.first_user_messages) < max(1, user_limit // 2):
                    pack.first_user_messages.append(trimmed)
                last_users.append(trimmed)

        pack.last_events = list(last_events)
        pack.errors = list(errors)
        pack.mutating_tools = list(mutating)
        pack.compactions = list(compactions)
        pack.last_user_messages = list(last_users)
        pack.redactions = self.redactor.count
        return pack

    def _trim(self, event: NormalizedEvent) -> NormalizedEvent:
        """Redact, drop credential-file payloads, and truncate loudly."""
        text = event.text or ""
        if any(Redactor.looks_like_credential_path(path) for path in event.file_paths):
            text = "[REDACTED: content of a file that looks like a credential store]"
        elif event.kind == KIND_TOOL_CALL and Redactor.looks_like_credential_path(text[:400]):
            text = "[REDACTED: tool call referencing a possible credential file]"
        else:
            text = self.redactor.scrub(text)

        limit = self.max_tool_text if event.kind in (
            KIND_TOOL_CALL, KIND_TOOL_RESULT, KIND_TOOL_ERROR) else self.max_text
        label = "tool result" if event.kind in (KIND_TOOL_RESULT, KIND_TOOL_ERROR) else "text"
        text = truncate_text(text, limit, label)

        return NormalizedEvent(
            timestamp=event.timestamp, agent=event.agent, session_id=event.session_id,
            kind=event.kind, role=event.role, text=text, tool_name=event.tool_name,
            file_paths=list(event.file_paths), is_error=event.is_error,
            raw_type=event.raw_type, mutating=event.mutating,
        )


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

NOT_DETERMINED = ("Not automatically determined without AI consolidation. "
                  "See `.handoff/conversation-tail.md`.")


def render_events(events: Sequence[NormalizedEvent]) -> str:
    if not events:
        return "_(none captured)_\n"
    chunks: List[str] = []
    for event in events:
        body = (event.text or "").strip()
        if not body:
            continue
        chunks.append("### %s\n\n%s\n" % (event.header(), body))
    if not chunks:
        return "_(none captured)_\n"
    return "\n".join(chunks)


def render_conversation_tail(pack: EvidencePack, generated_at: str) -> str:
    session = pack.session
    lines: List[str] = [
        "# Conversation evidence\n\n",
        "> Generated at %s\n" % generated_at,
        "> Source agent: %s\n" % AGENT_LABELS.get(session.agent, session.agent),
        "> Session: %s\n" % (session.session_id or "unknown"),
        "> Transcript: %s (%s, modified %s)\n" % (
            session.path, human_size(session.size), session.mtime_iso),
        "> Events parsed: %d\n" % pack.total_events,
    ]
    if pack.invalid_lines:
        lines.append("> Invalid JSONL lines ignored: %d\n" % pack.invalid_lines)
    if pack.redactions:
        lines.append("> Possible secrets redacted: %d\n" % pack.redactions)
    lines.append("\nThis file is evidence, not conclusions. Text below is quoted from the\n"
                 "transcript and truncated where marked; the repository is authoritative.\n\n")

    lines.append("---\n\n## Beginning of session\n\n")
    lines.append(render_events(pack.first_events))

    if pack.compactions:
        lines.append("\n---\n\n## Compaction / summary events\n\n")
        lines.append(render_events(pack.compactions))

    if pack.first_user_messages or pack.last_user_messages:
        lines.append("\n---\n\n## User messages\n\n")
        lines.append("### Earliest\n\n")
        lines.append(render_events(pack.first_user_messages))
        lines.append("\n### Most recent\n\n")
        lines.append(render_events(pack.last_user_messages))

    if pack.errors:
        lines.append("\n---\n\n## Errors and failed tool activity\n\n")
        lines.append(render_events(pack.errors))

    if pack.mutating_tools:
        lines.append("\n---\n\n## File-mutating tool activity\n\n")
        lines.append(render_events(pack.mutating_tools))

    lines.append("\n---\n\n## Recent activity (end of session)\n\n")
    lines.append(render_events(pack.last_events))
    return collapse_blank_lines("".join(lines))


def render_session_meta(
    pack: EvidencePack, git: GitState, generated_at: str, digest: Optional[str]
) -> str:
    session = pack.session
    data = {
        "generated_at": generated_at,
        "agent": session.agent,
        "session_id": session.session_id,
        "source_path": str(session.path),
        "source_size": session.size,
        "source_mtime": session.mtime_iso,
        "source_sha256": digest,
        "session_cwd": session.cwd or pack.session_cwd,
        "session_match": session.match,
        "repository": git.root,
        "branch": git.branch,
        "head": git.head,
        "events_parsed": pack.total_events,
        "events_selected": pack.selected_count(),
        "invalid_lines": pack.invalid_lines,
        "redactions": pack.redactions,
        "event_kinds": dict(pack.kind_counts),
        "top_tools": dict(pack.tool_usage.most_common(15)),
    }
    return json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def _relevant_files(pack: EvidencePack, git: GitState) -> List[str]:
    """Files Git currently reports as changed, marked when the session also touched them."""
    root = normalize_path(git.root)
    touched: Dict[str, int] = {}
    for raw, count in pack.touched_files.items():
        normalized = normalize_path(raw)
        if root and normalized.startswith(root + "/"):
            touched[normalized[len(root) + 1:]] = count
        elif not re.match(r"^([a-z]:)?/", normalized):
            touched[normalized] = count  # already repository-relative
        # Absolute paths outside the repository are ignored: scratchpads,
        # temp files and other checkouts are not relevant to this handoff.

    changed = git.changed_paths()
    changed_normalized = {normalize_path(path) for path in changed}

    lines: List[str] = []
    for path in changed[:60]:
        marker = " - touched by the session" if normalize_path(path) in touched else ""
        lines.append("- `%s`%s" % (path, marker))

    extra = [path for path in touched if path not in changed_normalized]
    for path in sorted(extra)[:20]:
        lines.append("- `%s` - edited during the session, currently clean in Git" % path)
    return lines


def render_deterministic_handoff(
    pack: EvidencePack, git: GitState, generated_at: str, ai_body: Optional[str] = None
) -> str:
    session = pack.session
    agent_label = AGENT_LABELS.get(session.agent, session.agent)

    header = [
        "# Current Handoff\n\n",
        "> Generated at %s\n" % generated_at,
        "> Source: %s\n" % agent_label,
        "> Session: %s\n" % (session.session_id or "unknown"),
        "> Repository: %s\n" % git.root,
        "> Branch: %s\n" % git.branch,
        "> HEAD: %s%s\n" % (git.head, (" - " + git.head_subject) if git.head_subject else ""),
        "> Mode: %s\n" % ("AI-consolidated" if ai_body else "deterministic (no AI)"),
        "\n",
    ]

    if ai_body:
        body = ai_body.strip() + "\n"
    else:
        first_user = pack.first_user_messages[0].text.strip() if pack.first_user_messages else ""
        last_user = pack.last_user_messages[-1].text.strip() if pack.last_user_messages else ""

        goal_lines = ["## Goal\n\n"]
        if first_user:
            goal_lines.append(
                "Not interpreted automatically. First user request recorded in this session,\n"
                "quoted verbatim (it may have been superseded later):\n\n")
            goal_lines.append("> " + truncate_text(first_user, 1200, "quote").replace("\n", "\n> ") + "\n")
        else:
            goal_lines.append(NOT_DETERMINED + "\n")
        if last_user and last_user != first_user:
            goal_lines.append("\nMost recent user message in this session:\n\n")
            goal_lines.append("> " + truncate_text(last_user, 1200, "quote").replace("\n", "\n> ") + "\n")

        state_lines = ["\n## Current State\n\n"]
        state_lines.append("Facts collected from Git and the transcript (no interpretation):\n\n")
        state_lines.append("- Working tree: %s\n" % (
            "%d path(s) with pending changes" % len(git.changed_paths())
            if git.is_dirty else "clean"))
        state_lines.append("- Branch `%s` at `%s`\n" % (git.branch, short_head(git.head)))
        state_lines.append("- Transcript events parsed: %d (%s)\n" % (
            pack.total_events,
            ", ".join("%s=%d" % (kind, count) for kind, count in pack.kind_counts.most_common(6))
            or "no events"))
        if pack.errors:
            state_lines.append("- Failed tool calls captured: %d (see Known Problems)\n" % len(pack.errors))
        if pack.compactions:
            state_lines.append("- Context compaction events in the session: %d\n" % len(pack.compactions))

        completed_lines = ["\n## Confirmed Completed Work\n\n"]
        completed_lines.append(
            "Not confirmed automatically. A transcript claim is not proof: verify with\n"
            "`git log`, `git diff` and the test suite before treating anything as done.\n\n")
        if git.recent_commits.strip():
            completed_lines.append("Commits already in history (these *are* confirmed):\n\n```\n%s\n```\n"
                                   % git.recent_commits.strip())

        files_lines = ["\n## Relevant Files\n\n"]
        relevant = _relevant_files(pack, git)
        files_lines.append("\n".join(relevant) + "\n" if relevant
                           else "No pending changes in the working tree and no file edits captured.\n")

        decisions_lines = ["\n## Technical Decisions\n\n", NOT_DETERMINED + "\n"]
        rejected_lines = ["\n## Failed / Rejected Approaches\n\n", NOT_DETERMINED + "\n"]

        problems_lines = ["\n## Known Problems\n\n"]
        if pack.errors:
            problems_lines.append(
                "%d failed tool call(s) were captured. Most recent, verbatim and truncated:\n\n"
                % len(pack.errors))
            for event in pack.errors[-3:]:
                problems_lines.append("- `%s`\n\n```\n%s\n```\n\n" % (
                    event.header(), truncate_text(event.text.strip(), 600, "error")))
            problems_lines.append("Full list: `.handoff/conversation-tail.md`.\n")
        else:
            problems_lines.append("No failed tool calls captured in the selected evidence.\n")
        if git.conflicted:
            problems_lines.append("\nGit reports conflicted paths: %s\n"
                                  % ", ".join("`%s`" % p for p in git.conflicted))

        pending_lines = ["\n## Pending Work\n\n", NOT_DETERMINED + "\n"]
        if git.is_dirty:
            pending_lines.append(
                "\nThe working tree is not clean, so work was in progress when the session\n"
                "ended. Uncommitted changes are listed under Relevant Files and detailed in\n"
                "`.handoff/git-state.txt`.\n")

        next_lines = ["\n## Suggested Next Steps\n\n"]
        next_lines.append("1. Run `git status` and `git diff` to see the real, current state.\n")
        next_lines.append("2. Read the files listed under Relevant Files before changing anything.\n")
        next_lines.append("3. Read `.handoff/conversation-tail.md` for what the previous agent was doing.\n")
        if pack.errors:
            next_lines.append("4. Reproduce the failures listed under Known Problems before assuming they are fixed.\n")

        questions_lines = ["\n## Unresolved Questions\n\n", NOT_DETERMINED + "\n"]

        body = "".join(goal_lines + state_lines + completed_lines + files_lines
                       + decisions_lines + rejected_lines + problems_lines
                       + pending_lines + next_lines + questions_lines)

    footer = ["\n## Git State\n\n"]
    footer.append("- Repository: `%s`\n" % git.root)
    footer.append("- Branch: `%s`\n" % git.branch)
    footer.append("- HEAD: `%s`\n" % git.head)
    footer.append("- Working tree: %s\n" % ("dirty" if git.is_dirty else "clean"))
    if git.status_porcelain.strip():
        status = git.status_porcelain.strip().splitlines()
        shown = "\n".join(status[:40])
        if len(status) > 40:
            shown += "\n... (%d more lines, see .handoff/git-state.txt)" % (len(status) - 40)
        footer.append("\n```\n%s\n```\n" % shown)
    if git.diff_stat.strip():
        footer.append("\nUnstaged diff stat:\n\n```\n%s\n```\n" % git.diff_stat.strip())
    footer.append("\nThe full diff is intentionally not included here. Run `git diff` in this checkout.\n")

    footer.append("\n## Evidence\n\n")
    footer.append("Detailed recent conversation evidence:\n`.handoff/conversation-tail.md`\n\n")
    footer.append("Detailed Git snapshot:\n`.handoff/git-state.txt`\n\n")
    footer.append("Snapshot metadata:\n`.handoff/session.json`\n\n")
    footer.append("Source transcript (read-only, never modified by this tool):\n`%s`\n" % session.path)

    footer.append("\n## Resume Instructions\n\n")
    footer.append(
        "1. Read `AGENTS.md` when it exists.\n"
        "2. Read this file.\n"
        "3. Inspect `git status`.\n"
        "4. Inspect `git diff`.\n"
        "5. Read the relevant source files.\n"
        "6. Consult `.handoff/conversation-tail.md` only when more historical context is needed.\n"
        "7. Preserve unfinished work already present in the working tree.\n"
        "8. Do not restart the implementation from scratch unless evidence proves the\n"
        "   existing approach is unusable.\n")

    return collapse_blank_lines("".join(header) + body + "".join(footer))


# --------------------------------------------------------------------------
# LLM client (optional)
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """Você está preparando um handoff entre dois agentes de programação.

Seu trabalho é interpretar evidências históricas de uma sessão anterior e produzir
um resumo operacional para o próximo agente continuar o trabalho.

FONTES DE VERDADE, em ordem:

1. Estado Git e filesystem atual.
2. Transcript real da sessão.
3. Sua própria interpretação.

Nunca inverta essa ordem.

Não presuma que algo foi implementado apenas porque foi discutido.
Não transforme uma intenção futura em trabalho concluído.
Não diga que um bug foi corrigido sem evidência suficiente.
Não diga que um teste passou sem evidência explícita.

Quando uma informação for incerta, marque: "não confirmado".

Identifique somente:

1. objetivo atual;
2. estado da implementação;
3. trabalho efetivamente concluído;
4. arquivos relevantes;
5. decisões técnicas tomadas;
6. abordagens tentadas e descartadas;
7. erros e problemas encontrados;
8. trabalho pendente;
9. próximos passos;
10. questões não resolvidas.

Diferencie claramente: discutido; tentado; implementado; testado; confirmado.

Não reproduza grandes trechos do transcript.
Não escreva introduções genéricas.
Não invente contexto.

Produza Markdown conciso, técnico e orientado à continuação do trabalho.

FORMATO OBRIGATÓRIO DA SAÍDA: comece diretamente em "## Goal" e use exatamente
estas seções de nível 2, nesta ordem, sem nenhum texto antes ou depois:

## Goal
## Current State
## Confirmed Completed Work
## Relevant Files
## Technical Decisions
## Failed / Rejected Approaches
## Known Problems
## Pending Work
## Suggested Next Steps
## Unresolved Questions
"""


class LLMClient:
    """Minimal OpenAI-compatible chat client built on urllib. No external deps."""

    def __init__(self, config: Dict[str, Any]) -> None:
        llm = config.get("llm") or {}
        self.base_url = str(llm.get("base_url") or "").rstrip("/")
        self.model = str(llm.get("model") or "")
        self.timeout = int(llm.get("timeout_seconds") or 120)
        self.temperature = llm.get("temperature", 0.1)
        self.api_key_env = str(llm.get("api_key_env") or "HANDOFF_LLM_API_KEY")
        self.max_input_chars = int(llm.get("max_input_chars") or 120000)

    @property
    def api_key(self) -> Optional[str]:
        return os.environ.get("HANDOFF_LLM_API_KEY") or os.environ.get(self.api_key_env)

    def _request(self, path: str, payload: Optional[Dict[str, Any]] = None,
                 timeout: Optional[int] = None) -> Any:
        if not self.base_url:
            raise HandoffError("no LLM base_url configured.", EXIT_LLM)
        url = "%s%s" % (self.base_url, path)
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        key = self.api_key
        if key:
            headers["Authorization"] = "Bearer %s" % key
        request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001 - diagnostics only
                pass
            raise HandoffError("LLM HTTP %s from %s%s" % (
                exc.code, url, (": " + detail) if detail else ""), EXIT_LLM)
        except urllib.error.URLError as exc:
            raise HandoffError("cannot reach LLM at %s (%s)" % (url, exc.reason), EXIT_LLM)
        except OSError as exc:
            raise HandoffError("cannot reach LLM at %s (%s)" % (url, exc), EXIT_LLM)
        try:
            return json.loads(raw)
        except ValueError:
            raise HandoffError("LLM returned a non-JSON response from %s" % url, EXIT_LLM)

    def list_models(self, timeout: int = 5) -> List[str]:
        payload = self._request("/v1/models", timeout=timeout)
        models: List[str] = []
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, list):
            for entry in data:
                if isinstance(entry, dict) and entry.get("id"):
                    models.append(str(entry["id"]))
        return models

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.temperature,
            "stream": False,
        }
        response = self._request("/v1/chat/completions", payload)
        if not isinstance(response, dict):
            raise HandoffError("unexpected LLM response shape.", EXIT_LLM)
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise HandoffError("LLM response contained no choices.", EXIT_LLM)
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = ""
        if isinstance(message, dict):
            content = message.get("content") or ""
            if not content and isinstance(message.get("reasoning_content"), str):
                content = message["reasoning_content"]
        elif isinstance(choices[0], dict):
            content = choices[0].get("text") or ""
        if not isinstance(content, str) or not content.strip():
            raise HandoffError("LLM returned empty content.", EXIT_LLM)
        return content.strip()


def build_ai_input(
    pack: EvidencePack, git: GitState, conversation_tail: str, git_state_text: str,
    redactor: Redactor, max_chars: int,
) -> str:
    """The exact user-role payload sent to the LLM, post-redaction."""
    session = pack.session
    header = [
        "# Handoff evidence pack\n\n",
        "## Snapshot metadata\n\n",
        "- Repository: %s\n" % git.root,
        "- Branch: %s\n" % git.branch,
        "- HEAD: %s\n" % git.head,
        "- Source agent: %s\n" % AGENT_LABELS.get(session.agent, session.agent),
        "- Session: %s\n" % (session.session_id or "unknown"),
        "- Transcript size: %s\n" % human_size(session.size),
        "- Events parsed: %d, selected for evidence: %d\n" % (
            pack.total_events, pack.selected_count()),
        "- Working tree: %s\n\n" % ("dirty" if git.is_dirty else "clean"),
        "## Current Git state (authoritative)\n\n```\n",
        redactor.scrub(git_state_text.strip()),
        "\n```\n\n",
        "## Conversation evidence (historical, NOT proof of implementation)\n\n",
    ]
    body = redactor.scrub(conversation_tail)
    text = "".join(header) + body

    if len(text) > max_chars:
        # Keep the head (metadata + git) and the tail (most recent evidence).
        keep_head = min(len(text), max(2000, max_chars // 3))
        keep_tail = max_chars - keep_head - 200
        text = (text[:keep_head]
                + "\n\n[...evidence pack truncated to fit the model input budget: "
                  "%d chars removed...]\n\n" % (len(text) - keep_head - keep_tail)
                + text[-keep_tail:])
    return text


# --------------------------------------------------------------------------
# Workspace paths
# --------------------------------------------------------------------------


@dataclass
class Workspace:
    root: Path

    @property
    def handoff_dir(self) -> Path:
        return self.root / HANDOFF_DIRNAME

    @property
    def history_dir(self) -> Path:
        return self.handoff_dir / HISTORY_DIRNAME

    @property
    def config_path(self) -> Path:
        return self.handoff_dir / CONFIG_FILENAME

    @property
    def session_meta_path(self) -> Path:
        return self.handoff_dir / SESSION_META_FILENAME

    @property
    def git_state_path(self) -> Path:
        return self.handoff_dir / GIT_STATE_FILENAME

    @property
    def conversation_tail_path(self) -> Path:
        return self.handoff_dir / CONVERSATION_TAIL_FILENAME

    @property
    def ai_preview_path(self) -> Path:
        return self.handoff_dir / AI_PREVIEW_FILENAME

    @property
    def handoff_path(self) -> Path:
        return self.root / HANDOFF_FILENAME


def target_repo(args: argparse.Namespace) -> Optional[Path]:
    """Which checkout to act on: --repo, else $HANDOFF_REPO, else the cwd.

    This is what lets one copy of the script serve every project: keep the tool
    in one folder and point it at the repository you are working in.
    """
    value = getattr(args, "repo", None) or os.environ.get("HANDOFF_REPO")
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_dir():
        raise HandoffError("--repo path is not a directory: %s" % path, EXIT_USAGE)
    return path


def ensure_git_exclude(root: Path) -> List[str]:
    """Add handoff rules to .git/info/exclude idempotently. Never touches .gitignore."""
    git_dir_output = subprocess.run(
        ["git", "rev-parse", "--git-dir"], cwd=str(root),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        encoding="utf-8", errors="replace")
    if git_dir_output.returncode != 0:
        raise HandoffError("cannot locate the .git directory for %s" % root, EXIT_GIT)
    git_dir = Path(git_dir_output.stdout.strip())
    if not git_dir.is_absolute():
        git_dir = root / git_dir
    info_dir = git_dir / "info"
    exclude_path = info_dir / "exclude"

    existing = ""
    if exclude_path.is_file():
        try:
            with open(exclude_path, "r", encoding="utf-8", errors="replace") as handle:
                existing = handle.read()
        except OSError as exc:
            raise HandoffError("cannot read %s: %s" % (exclude_path, exc), EXIT_GIT)

    # Upgrade an unanchored rule written by an older version, in place, so
    # nothing else in the file moves.
    changed: List[str] = []
    lines = existing.splitlines()
    for index, line in enumerate(lines):
        anchored = LEGACY_EXCLUDE_RULES.get(line.strip())
        if anchored:
            lines[index] = anchored
            changed.append("%s -> %s" % (line.strip(), anchored))
    if changed:
        existing = "\n".join(lines) + "\n"

    present = {line.strip() for line in existing.splitlines()}
    missing = [rule for rule in EXCLUDE_RULES if rule not in present]
    if not missing and not changed:
        return []

    info_dir.mkdir(parents=True, exist_ok=True)
    addition = ""
    if missing:
        if existing and not existing.endswith("\n"):
            addition += "\n"
        if "# handoff tool (tools/handoff.py)" not in existing:
            addition += "\n# handoff tool (tools/handoff.py) - local artifacts, never committed\n"
        addition += "\n".join(missing) + "\n"
    atomic_write(exclude_path, existing + addition)
    return changed + missing


AGENTS_MD_SECTION = """
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
"""


def update_agents_md(root: Path) -> str:
    """Append the handoff section to an existing AGENTS.md. Never creates the file."""
    path = root / "AGENTS.md"
    if not path.is_file():
        return "absent"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read()
    except OSError as exc:
        warn("cannot read AGENTS.md: %s" % exc)
        return "unreadable"
    if re.search(r"(?im)^#{1,6}\s*Agent handoff\s*$", content):
        return "already present"
    if "HANDOFF.md" in content:
        return "already mentions HANDOFF.md; left untouched"
    separator = "" if content.endswith("\n") else "\n"
    atomic_write(path, content + separator + AGENTS_MD_SECTION)
    return "section appended"


# --------------------------------------------------------------------------
# Snapshot pipeline
# --------------------------------------------------------------------------


def archive_handoff(workspace: Workspace, generated_at: str, session: SessionInfo,
                    content: str) -> Path:
    workspace.history_dir.mkdir(parents=True, exist_ok=True)
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})", generated_at or "")
    if match:
        pretty = "%s-%s-%s_%s%s%s" % match.groups()
    else:
        pretty = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    # session_id comes straight from the transcript's JSON - sanitize it before
    # it becomes part of a filename, so a crafted --session-file can't smuggle
    # "../" through here (session.agent is normally fixed by this tool, but
    # costs nothing to sanitize the same way).
    safe_agent = safe_filename_component(session.agent, "agent")
    safe_session = safe_filename_component(short_id(session.session_id), "session")
    name = "%s_%s_%s.md" % (pretty, safe_agent, safe_session)
    target = workspace.history_dir / name
    counter = 1
    while target.exists():
        target = workspace.history_dir / ("%s_%s_%s_%d.md" % (
            pretty, safe_agent, safe_session, counter))
        counter += 1
    atomic_write(target, content)
    return target


def preserve_previous_handoff(workspace: Workspace) -> Optional[Path]:
    """Keep the outgoing HANDOFF.md if history does not already contain it."""
    path = workspace.handoff_path
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read()
    except OSError:
        return None
    if not content.strip():
        return None
    digest = sha256_text(content)
    if workspace.history_dir.is_dir():
        for entry in workspace.history_dir.glob("*.md"):
            try:
                with open(entry, "r", encoding="utf-8", errors="replace") as handle:
                    if sha256_text(handle.read()) == digest:
                        return None
            except OSError:
                continue
    stamp = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d_%H%M%S")
    target = workspace.history_dir / ("%s_replaced.md" % stamp)
    if target.exists():
        return None
    workspace.history_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(target, content)
    return target


def run_snapshot(args: argparse.Namespace, mode: str) -> int:
    """Shared implementation behind `snapshot` and `recover`."""
    inspector = GitInspector(target_repo(args))
    root = inspector.find_root()
    workspace = Workspace(root)
    config = load_config(workspace.config_path)

    agent = args.agent
    adapter = get_adapter(agent, root)
    label = AGENT_LABELS[agent]

    print("Repository: %s" % root)
    print("Agent: %s" % label)
    if mode == "recover":
        print("Mode: recover (session ended without a handoff)")

    if not adapter.available() and not args.session_file:
        raise HandoffError(
            "%s session directory not found: %s\n"
            "Is %s installed for this user?" % (label, adapter.root_dir(), label),
            EXIT_NO_SESSION)

    session = adapter.select(args.session, args.session_file)
    print("\nUsing %s session:\n\n%s" % (label, session.describe()))
    if session.note:
        print("  match: %s (%s)" % (session.match, session.note))
    if session.match == "foreign":
        warn("this session records cwd=%s, which is outside %s." % (session.cwd, root))
    elif session.match == "hinted":
        warn("no cwd found inside the session; matched by directory name only.")

    print("\nCollecting Git state...")
    git = inspector.collect()

    print("Parsing session...")
    redactor = Redactor()
    builder = EvidenceBuilder(config, redactor)
    pack = builder.build(adapter, session)
    print("Parsed %d events; selected %d for evidence." % (pack.total_events, pack.selected_count()))
    if pack.invalid_lines:
        warn("%d invalid JSONL line(s) ignored." % pack.invalid_lines)
    if pack.total_events == 0:
        warn("no usable events found in this transcript; the handoff will be Git-only.")

    generated_at = now_iso()
    print("Building evidence pack...")
    conversation_tail = render_conversation_tail(pack, generated_at)
    git_state_text = render_git_state(git, generated_at)

    ai_body: Optional[str] = None
    if args.ai:
        ai_body = consolidate_with_ai(
            workspace, config, pack, git, conversation_tail, git_state_text,
            redactor, dry_run=args.dry_run)

    if redactor.count:
        print("Redacted %d possible secret(s)." % redactor.count)

    digest = sha256_file(session.path)
    handoff_text = render_deterministic_handoff(pack, git, generated_at, ai_body)

    preserved = preserve_previous_handoff(workspace)
    workspace.handoff_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(workspace.git_state_path, git_state_text)
    atomic_write(workspace.conversation_tail_path, conversation_tail)
    atomic_write(workspace.session_meta_path,
                 render_session_meta(pack, git, generated_at, digest))
    atomic_write(workspace.handoff_path, handoff_text)
    history_path = archive_handoff(workspace, generated_at, session, handoff_text)

    if preserved:
        print("Previous handoff kept: %s" % preserved.relative_to(root))
    print("Handoff written: %s" % HANDOFF_FILENAME)
    print("History: %s" % history_path.relative_to(root))
    return EXIT_OK


def consolidate_with_ai(
    workspace: Workspace, config: Dict[str, Any], pack: EvidencePack, git: GitState,
    conversation_tail: str, git_state_text: str, redactor: Redactor, dry_run: bool,
) -> Optional[str]:
    """Returns the AI-written body, or None to fall back to the deterministic one."""
    if not llm_is_configured(config) and not dry_run:
        # No integration available: say so once, plainly, and never touch the network.
        print("AI consolidation skipped: %s." % llm_skip_reason(config))
        print("Producing the deterministic handoff instead.")
        return None

    client = LLMClient(config)
    user_prompt = build_ai_input(pack, git, conversation_tail, git_state_text,
                                 redactor, client.max_input_chars)

    configured = llm_is_configured(config)
    preview = ("# AI input preview\n\n"
               "> Generated at %s\n"
               "> Endpoint: %s\n"
               "> Model: %s\n"
               "> Payload size: %d chars\n"
               "> Redactions applied: %d\n\n"
               "This is exactly what would be sent, after redaction. No credentials or\n"
               "API keys appear here: the Authorization header is never rendered.\n\n"
               "---\n\n## System prompt\n\n```\n%s\n```\n\n---\n\n## User message\n\n%s\n"
               % (now_iso(),
                  ("%s/v1/chat/completions" % client.base_url) if configured
                  else "(none - %s)" % llm_skip_reason(config),
                  client.model or "(none)", len(user_prompt),
                  redactor.count, SYSTEM_PROMPT.strip(), user_prompt))
    workspace.handoff_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(workspace.ai_preview_path, preview)
    print("AI input preview: %s" % workspace.ai_preview_path.relative_to(workspace.root))

    if dry_run:
        print("Dry run: the model was NOT called.")
        if not configured:
            print("Nothing would be sent anyway: %s." % llm_skip_reason(config))
        return None

    print("Calling %s..." % client.model)
    try:
        body = client.complete(SYSTEM_PROMPT, user_prompt)
    except HandoffError as exc:
        warn("AI consolidation failed: %s\nFalling back to deterministic handoff." % exc)
        return None
    body = redactor.scrub(body)
    if not body.lstrip().startswith("#"):
        body = "## Goal\n\n" + body
    print("AI consolidation received (%d chars)." % len(body))
    return body


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    inspector = GitInspector(target_repo(args))
    root = inspector.find_root()
    workspace = Workspace(root)

    workspace.handoff_dir.mkdir(parents=True, exist_ok=True)
    workspace.history_dir.mkdir(parents=True, exist_ok=True)
    print("Repository: %s" % root)
    print("Created:    %s/ and %s/%s/" % (HANDOFF_DIRNAME, HANDOFF_DIRNAME, HISTORY_DIRNAME))

    if workspace.config_path.is_file():
        print("Config:     %s already exists; left untouched." % workspace.config_path.name)
    else:
        atomic_write(workspace.config_path,
                     json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False) + "\n")
        print("Config:     %s created with defaults." % workspace.config_path.name)

    added = ensure_git_exclude(root)
    if added:
        print("Git:        added to .git/info/exclude: %s" % ", ".join(added))
    else:
        print("Git:        .git/info/exclude already covers the handoff artifacts.")

    status = update_agents_md(root)
    if status == "absent":
        print("AGENTS.md:  not found. Not created automatically - tell me if you want one,\n"
              "            or add the 'Agent handoff' section yourself (see docs/handoff.md).")
    else:
        print("AGENTS.md:  %s" % status)

    print("\nDiagnostics:")
    config = load_config(workspace.config_path)
    for agent in AGENTS:
        adapter = get_adapter(agent, root)
        if not adapter.available():
            print("  %-7s not installed for this user (%s missing)" % (
                AGENT_LABELS[agent] + ":", adapter.root_dir()))
            continue
        try:
            sessions = adapter.discover(probe_limit=10)
        except HandoffError as exc:
            print("  %-7s %s" % (AGENT_LABELS[agent] + ":", exc))
            continue
        linked = [s for s in sessions if s.match in ("confirmed", "hinted")]
        print("  %-7s %d session(s) found, %d linked to this repository" % (
            AGENT_LABELS[agent] + ":", len(sessions), len(linked)))

    print("\nNext:")
    print("  python3 tools/handoff.py doctor")
    print("  python3 tools/handoff.py snapshot claude")
    print("  python3 tools/handoff.py recover claude --ai")
    return EXIT_OK


def _agent_report(root: Path, agent: str, detailed: bool) -> None:
    label = AGENT_LABELS[agent]
    adapter = get_adapter(agent, root)
    print("\n%s" % label)
    if not adapter.available():
        print("  status: not found (%s)" % adapter.root_dir())
        return
    try:
        sessions = adapter.discover()
    except HandoffError as exc:
        print("  status: ERROR (%s)" % exc)
        return
    if not sessions:
        print("  status: directory exists but contains no sessions")
        print("  path: %s" % adapter.root_dir())
        return

    linked = [s for s in sessions if s.match in ("confirmed", "hinted")]
    print("  status: OK")
    print("  sessions: %d total, %d linked to this repository" % (len(sessions), len(linked)))
    newest = linked[0] if linked else sessions[0]
    print("  latest%s: %s" % (" (linked)" if linked else " (unlinked)", newest.session_id))
    print("  modified: %s" % newest.mtime_iso)
    print("  size: %s" % human_size(newest.size))
    if detailed:
        print("  path: %s" % newest.path)
        if newest.cwd:
            print("  cwd: %s" % newest.cwd)
        print("  match: %s (%s)" % (newest.match, newest.note))
        others = [s for s in linked[1:4]]
        if others:
            print("  other linked sessions:")
            for info in others:
                print("    %s  %s  %s" % (info.session_id, info.mtime_iso, human_size(info.size)))


def cmd_doctor(args: argparse.Namespace) -> int:
    print("Environment")
    print("  python: %s" % sys.version.split()[0])
    print("  platform: %s" % sys.platform)

    inspector = GitInspector(target_repo(args))
    try:
        code, version = inspector.run(["--version"])
        print("  git: %s" % (version.strip() if code == 0 else "unavailable"))
    except HandoffError as exc:
        print("  git: %s" % exc)
        return EXIT_GIT

    root = inspector.find_root()
    workspace = Workspace(root)
    git = inspector.collect()
    print("\nRepository")
    print("  root: %s" % git.root)
    print("  branch: %s" % git.branch)
    print("  head: %s" % short_head(git.head))
    print("  working tree: %s" % ("dirty (%d path(s))" % len(git.changed_paths())
                                  if git.is_dirty else "clean"))
    print("  handoff dir: %s" % ("present" if workspace.handoff_dir.is_dir()
                                 else "missing (run `init`)"))

    for agent in AGENTS:
        _agent_report(root, agent, detailed=True)

    config = load_config(workspace.config_path)
    llm = config.get("llm") or {}
    base_url = str(llm.get("base_url") or "")
    model = str(llm.get("model") or "")

    print("\nAI consolidation (optional)")
    if not llm_is_configured(config):
        print("  status: off - %s" % (
            'switched off by "enabled": false' if llm.get("enabled") is False
            else "no LLM integration configured"))
        print("  effect: --ai still works and produces a deterministic handoff")
        print("  to enable: %s" % llm_hint(config))
        return EXIT_OK

    print("  status: configured")
    print("  endpoint: %s" % base_url)
    print("  model: %s" % model)
    key_env = str(llm.get("api_key_env") or "HANDOFF_LLM_API_KEY")
    print("  api key: %s (value never displayed)" % (
        "set via $%s" % key_env if os.environ.get(key_env) else "not set (may be unnecessary)"))
    client = LLMClient(config)
    try:
        models = client.list_models(timeout=5)
    except HandoffError as exc:
        print("  reachable: no (%s)" % exc)
        print("  note: AI is optional; every other command works without it.")
    else:
        print("  reachable: yes")
        if models:
            print("  models exposed: %s" % ", ".join(models[:8]))
            if model not in models:
                warn("configured model '%s' is not in the endpoint's model list." % model)
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    inspector = GitInspector(target_repo(args))
    root = inspector.find_root()
    workspace = Workspace(root)
    git = inspector.collect()

    print("Repository: %s" % git.root)
    print("Branch:     %s" % git.branch)
    print("Commit:     %s%s" % (short_head(git.head),
                                (" - " + git.head_subject) if git.head_subject else ""))
    if git.is_dirty:
        counts = []
        for name, values in (("modified", git.modified), ("added", git.added),
                             ("deleted", git.deleted), ("renamed", git.renamed),
                             ("untracked", git.untracked), ("conflicted", git.conflicted)):
            if values:
                counts.append("%d %s" % (len(values), name))
        print("Work tree:  dirty (%s)" % ", ".join(counts))
    else:
        print("Work tree:  clean")

    if workspace.handoff_path.is_file():
        print("Handoff:    %s (updated %s)" % (HANDOFF_FILENAME,
                                               mtime_iso(workspace.handoff_path)))
        if workspace.session_meta_path.is_file():
            try:
                with open(workspace.session_meta_path, "r", encoding="utf-8") as handle:
                    meta = json.load(handle)
                print("            from %s session %s" % (
                    AGENT_LABELS.get(meta.get("agent"), meta.get("agent")),
                    short_id(meta.get("session_id"))))
            except (OSError, ValueError):
                pass
    else:
        print("Handoff:    none yet (run `snapshot` or `recover`)")

    for agent in AGENTS:
        label = AGENT_LABELS[agent]
        adapter = get_adapter(agent, root)
        if not adapter.available():
            print("%-11s not installed" % (label + ":"))
            continue
        try:
            sessions = adapter.discover(probe_limit=20)
        except HandoffError:
            sessions = []
        linked = [s for s in sessions if s.match in ("confirmed", "hinted")]
        if linked:
            newest = linked[0]
            print("%-11s %s  %s  %s" % (label + ":", short_id(newest.session_id),
                                        newest.mtime_iso, human_size(newest.size)))
        elif sessions:
            print("%-11s %d session(s), none linked to this repository" % (label + ":", len(sessions)))
        else:
            print("%-11s no sessions" % (label + ":"))
    return EXIT_OK


def cmd_snapshot(args: argparse.Namespace) -> int:
    return run_snapshot(args, "snapshot")


def cmd_recover(args: argparse.Namespace) -> int:
    return run_snapshot(args, "recover")


def cmd_consolidate(args: argparse.Namespace) -> int:
    """Rebuild HANDOFF.md from the existing snapshot, without re-reading the transcript."""
    inspector = GitInspector(target_repo(args))
    root = inspector.find_root()
    workspace = Workspace(root)
    config = load_config(workspace.config_path)

    if not workspace.session_meta_path.is_file():
        raise HandoffError(
            "no snapshot found (%s missing).\nRun `snapshot` or `recover` first."
            % workspace.session_meta_path, EXIT_USAGE)
    try:
        with open(workspace.session_meta_path, "r", encoding="utf-8") as handle:
            meta = json.load(handle)
    except (OSError, ValueError) as exc:
        raise HandoffError("cannot read %s: %s" % (workspace.session_meta_path, exc), EXIT_USAGE)

    conversation_tail = ""
    if workspace.conversation_tail_path.is_file():
        with open(workspace.conversation_tail_path, "r", encoding="utf-8", errors="replace") as handle:
            conversation_tail = handle.read()
    else:
        warn("%s is missing; consolidating from Git state only."
             % workspace.conversation_tail_path.name)

    agent = str(meta.get("agent") or AGENT_CLAUDE)
    source_path = Path(str(meta.get("source_path") or ""))
    session = SessionInfo(
        agent=agent,
        session_id=str(meta.get("session_id") or ""),
        path=source_path,
        mtime=source_path.stat().st_mtime if source_path.is_file() else 0.0,
        size=int(meta.get("source_size") or 0),
        cwd=meta.get("session_cwd"),
        match="from snapshot",
        note="reused from %s" % SESSION_META_FILENAME,
    )
    pack = EvidencePack(session=session)
    pack.total_events = int(meta.get("events_parsed") or 0)
    pack.invalid_lines = int(meta.get("invalid_lines") or 0)
    kinds = meta.get("event_kinds")
    if isinstance(kinds, dict):
        pack.kind_counts = Counter({str(k): int(v) for k, v in kinds.items()})

    print("Repository: %s" % root)
    print("Reusing snapshot of %s session %s" % (
        AGENT_LABELS.get(agent, agent), short_id(session.session_id)))
    print("Refreshing Git state...")
    git = inspector.collect()
    generated_at = now_iso()
    git_state_text = render_git_state(git, generated_at)

    redactor = Redactor()
    ai_body: Optional[str] = None
    if args.ai:
        ai_body = consolidate_with_ai(
            workspace, config, pack, git, conversation_tail, git_state_text,
            redactor, dry_run=args.dry_run)
    else:
        print("No --ai given: rebuilding the deterministic handoff from the stored snapshot.")

    handoff_text = render_deterministic_handoff(pack, git, generated_at, ai_body)
    if ai_body is None and not conversation_tail:
        warn("without --ai and without conversation evidence this only refreshes Git facts.")

    preserved = preserve_previous_handoff(workspace)
    atomic_write(workspace.git_state_path, git_state_text)
    atomic_write(workspace.handoff_path, handoff_text)
    history_path = archive_handoff(workspace, generated_at, session, handoff_text)
    if preserved:
        print("Previous handoff kept: %s" % preserved.relative_to(root))
    print("Handoff written: %s" % HANDOFF_FILENAME)
    print("History: %s" % history_path.relative_to(root))
    return EXIT_OK


HISTORY_NAME_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})_(?P<time>\d{6})_(?P<agent>[a-z]+)_(?P<session>[0-9a-zA-Z]+)")


def cmd_history(args: argparse.Namespace) -> int:
    inspector = GitInspector(target_repo(args))
    root = inspector.find_root()
    workspace = Workspace(root)
    if not workspace.history_dir.is_dir():
        print("No history yet (%s does not exist)." % workspace.history_dir)
        return EXIT_OK
    entries = sorted(workspace.history_dir.glob("*.md"), reverse=True)
    if not entries:
        print("No history entries in %s." % workspace.history_dir)
        return EXIT_OK

    print("%-21s  %-8s  %-10s  %s" % ("TIMESTAMP", "AGENT", "SESSION", "FILE"))
    for entry in entries[:args.limit]:
        match = HISTORY_NAME_RE.match(entry.name)
        if match:
            stamp = "%s %s:%s:%s" % (match.group("date"), match.group("time")[0:2],
                                     match.group("time")[2:4], match.group("time")[4:6])
            agent = match.group("agent")
            session = match.group("session")
        else:
            stamp = mtime_iso(entry)[:19].replace("T", " ")
            agent = "-"
            session = "-"
        print("%-21s  %-8s  %-10s  %s" % (
            stamp, agent, session, entry.relative_to(root)))
    if len(entries) > args.limit:
        print("\n... %d older entries. Use --limit N, or grep %s."
              % (len(entries) - args.limit, workspace.history_dir))
    return EXIT_OK


def cmd_show(args: argparse.Namespace) -> int:
    inspector = GitInspector(target_repo(args))
    root = inspector.find_root()
    workspace = Workspace(root)
    path = workspace.handoff_path
    if not path.is_file():
        raise HandoffError(
            "no %s in %s yet.\nRun `snapshot <agent>` or `recover <agent>` first."
            % (HANDOFF_FILENAME, root), EXIT_USAGE)
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        sys.stdout.write(handle.read())
    return EXIT_OK


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="handoff.py",
        description="Generate a handoff between Claude Code and OpenAI Codex "
                    "from the transcripts they already write.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Typical use:\n"
               "  python3 tools/handoff.py init\n"
               "  python3 tools/handoff.py doctor\n"
               "  python3 tools/handoff.py snapshot claude --ai   # planned switch\n"
               "  python3 tools/handoff.py recover claude --ai    # session died\n"
               "\n"
               "One copy, every project - keep this script in one place and point it\n"
               "at the checkout you are working in:\n"
               "  python3 tools/handoff.py recover claude --repo /path/to/project\n"
               "  export HANDOFF_REPO=/path/to/project   # then omit --repo\n",
    )

    # Accepted before the subcommand (global) and after it (per-subcommand),
    # because both read naturally: `--repo X doctor` and `doctor --repo X`.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo", metavar="PATH", default=argparse.SUPPRESS,
                        help="repository to act on (default: current directory, "
                             "or $HANDOFF_REPO)")
    common.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS,
                        help="print debug output to stderr")

    parser.add_argument("--repo", metavar="PATH", default=None,
                        help="repository to act on (default: current directory, "
                             "or $HANDOFF_REPO)")
    parser.add_argument("--verbose", action="store_true", help="print debug output to stderr")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init", parents=[common],
                   help="create .handoff/, config and git exclude rules").set_defaults(func=cmd_init)
    sub.add_parser("doctor", parents=[common],
                   help="diagnose repository, agents and AI endpoint").set_defaults(func=cmd_doctor)
    sub.add_parser("status", parents=[common],
                   help="short summary of repo, handoff and sessions").set_defaults(func=cmd_status)

    def add_session_flags(target: argparse.ArgumentParser) -> None:
        target.add_argument("agent", choices=list(AGENTS), help="source agent")
        target.add_argument("--session", metavar="ID",
                            help="session id (or unique prefix) to use")
        target.add_argument("--session-file", metavar="PATH",
                            help="explicit transcript path, bypassing discovery")
        target.add_argument("--ai", action="store_true",
                            help="consolidate the evidence pack with the LLM, when one "
                                 "is configured; falls back cleanly when none is")
        target.add_argument("--dry-run", action="store_true",
                            help="with --ai: write the input preview but never call the model")

    snapshot = sub.add_parser("snapshot", parents=[common],
                              help="capture the current state (normal use)")
    add_session_flags(snapshot)
    snapshot.set_defaults(func=cmd_snapshot)

    recover = sub.add_parser("recover", parents=[common],
                             help="rebuild a handoff after a session died")
    add_session_flags(recover)
    recover.set_defaults(func=cmd_recover)

    consolidate = sub.add_parser(
        "consolidate", parents=[common],
        help="rebuild HANDOFF.md from the existing snapshot")
    consolidate.add_argument("--ai", action="store_true",
                             help="use the LLM, when one is configured")
    consolidate.add_argument("--dry-run", action="store_true",
                             help="with --ai: write the input preview but never call the model")
    consolidate.set_defaults(func=cmd_consolidate)

    history = sub.add_parser("history", parents=[common], help="list archived handoffs")
    history.add_argument("--limit", type=int, default=20, help="entries to show (default 20)")
    history.set_defaults(func=cmd_history)

    sub.add_parser("show", parents=[common],
                   help="print the current HANDOFF.md").set_defaults(func=cmd_show)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    global VERBOSE
    parser = build_parser()
    args = parser.parse_args(argv)
    VERBOSE = bool(getattr(args, "verbose", False))

    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_USAGE

    try:
        return int(args.func(args))
    except HandoffError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return exc.code
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted\n")
        return EXIT_ERROR
    except BrokenPipeError:
        return EXIT_OK
    except Exception as exc:  # noqa: BLE001 - last resort, keep the trace behind --verbose
        if VERBOSE:
            raise
        sys.stderr.write("error: unexpected failure: %s: %s\n" % (type(exc).__name__, exc))
        sys.stderr.write("Run again with --verbose for the full traceback.\n")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
