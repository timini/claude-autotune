"""Explicit, sanitized export of one captured episode as a fixture (M7).

Nothing is exported unless the user names the episode. The output has the
shape of a calibration example (calibration/example.schema.json) with empty
labels, so it can be labelled and added to a private calibration set. It is
never written into the repository by Autotune.

Sanitizing is best effort, applied on top of the redaction done at capture:
evidence content and every other string are redacted again with
`redact.redact`; absolute paths inside the repository become relative to its
root; every other absolute path (home, temp, system) becomes
"<redacted-path>"; session ids are replaced by a short hash wherever they
appear; the transcript path and the repository location are dropped. An
episode with any expired evidence is refused, because the fixture would not
show what the reviewer saw. Expired means by the retention policy
(retain_until passed, or the episode older than raw_retention_days), whether
or not the worker's retention pass has cleared it yet. The caller gets a
report of what was removed.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from . import redact, retention
from .util import iso, sha256_text

FORMAT = "autotune.episode-export/1"
REDACTED_PATH = "<redacted-path>"

_REPOSITORY_KEYS = ("branch", "head", "tracked_dirty")
_PROVENANCE_KEYS = ("source", "field", "tool", "is_error", "denied", "index")

_SEG = r"[A-Za-z0-9._@%+=~-]+"
# A POSIX absolute path not inside a word, URL or relative path. A path after
# ':' counts (PATH lists, "file:/x", "a.py:/x"); in a "scheme://host/path" URL
# the first '/' is followed by '/' and the rest follow a word or '/', so URLs
# are left alone.
_ABS_PATH = re.compile(rf"(?<![\w./\\~$-])/{_SEG}(?:/{_SEG})*/?")
_HOME_PATH = re.compile(r"(?<![\w/~-])~[A-Za-z0-9._-]*/[A-Za-z0-9._@%+=~/-]*")
_FILE_URL = re.compile(r"(?i)\bfile://[^\s\"'`<>]*")
_WINDOWS_PATH = re.compile(r"(?<![\w])[A-Za-z]:\\{1,2}[^\s\"'`<>]*")


class ExportError(Exception):
    """CLI-only failure (never a job error code): episode_not_found,
    evidence_expired or output_exists."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def hash_id(value: str) -> str:
    return "sha256:" + sha256_text(value)[:16]


class Sanitizer:
    """Rewrites strings and counts what it removed."""

    def __init__(self, roots: list[str], ids: list[str]) -> None:
        variants = set()
        for root in roots:
            if root:
                root = root.rstrip("/")
                variants.update({root, os.path.realpath(root)})
        variants.discard("")
        variants.discard("/")
        # Longest first, so a nested worktree is matched before its parent.
        self._roots = [re.compile(re.escape(r) + r"(/|(?![\w.-]))") for r in sorted(variants, key=len, reverse=True)]
        self._ids = sorted({i for i in ids if i}, key=len, reverse=True)
        self.counts = {"secrets_redacted": 0, "session_ids_hashed": 0, "repo_paths_relativized": 0,
                       "paths_redacted": 0}

    def text(self, value: str) -> str:
        value, n = redact.redact(value)
        self.counts["secrets_redacted"] += n
        for sid in self._ids:
            n = value.count(sid)
            if n:
                value = value.replace(sid, hash_id(sid))
                self.counts["session_ids_hashed"] += n
        for pattern in self._roots:
            value, n = pattern.subn(lambda m: "" if m.group(1) == "/" else ".", value)
            self.counts["repo_paths_relativized"] += n
        for pattern in (_FILE_URL, _WINDOWS_PATH, _HOME_PATH, _ABS_PATH):
            value, n = pattern.subn(REDACTED_PATH, value)
            self.counts["paths_redacted"] += n
        return value

    def value(self, obj):
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, list):
            return [self.value(v) for v in obj]
        if isinstance(obj, dict):
            return {k: self.value(v) for k, v in obj.items()}
        return obj


def _loads(text, default):
    try:
        value = json.loads(text) if text is not None else default
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def build_export(conn, episode_id: str, repo: str | None, now: datetime, *,
                 raw_retention_days: int | None = None) -> tuple[dict, dict]:
    """Return (fixture, report) for one episode. Raises ExportError.
    raw_retention_days is the project's setting; evidence past it (or past
    its retain_until) is refused even before retention clears it."""
    ep = conn.execute("SELECT * FROM episodes WHERE episode_id = ?", (episode_id,)).fetchone()
    if ep is None:
        raise ExportError("episode_not_found", f"no episode {episode_id}")
    rows = conn.execute(
        "SELECT * FROM evidence WHERE episode_id = ? ORDER BY position", (episode_id,)
    ).fetchall()
    expired = [r["evidence_id"] for r in rows if r["expired_at"] is not None or r["content"] is None
               or retention.evidence_expired(r["retain_until"], ep["captured_at"], now, raw_retention_days)]
    if expired:
        raise ExportError("evidence_expired",
                          f"evidence {', '.join(expired[:20])} of {episode_id} has expired; nothing was exported")

    repository = _loads(ep["repository_json"], {})
    roots = [repo, repository.get("toplevel"), repository.get("git_common_dir")]
    session_ids = [ep["session_id"], ep["lifecycle_id"]]
    for r in conn.execute("SELECT DISTINCT session_id FROM reviews WHERE episode_id = ?", (episode_id,)):
        session_ids.append(r["session_id"])
    clean = Sanitizer([r for r in roots if isinstance(r, str)], [s for s in session_ids if isinstance(s, str)])

    dropped = ["episode.transcript_path"] if ep["transcript_path"] else []
    dropped += [f"episode.repository.{k}" for k in sorted(repository) if k not in _REPOSITORY_KEYS
                and repository.get(k) not in (None, [], "")]
    evidence = []
    for r in rows:
        prov = _loads(r["provenance_json"], {})
        evidence.append({
            "id": r["evidence_id"],
            "kind": r["kind"],
            "role": r["role"],
            "content": clean.text(r["content"]),
            "truncated": bool(r["truncated"]),
            "provenance": clean.value({k: prov[k] for k in _PROVENANCE_KEYS if k in prov}),
        })
    review = conn.execute(
        "SELECT review_json FROM reviews WHERE episode_id = ? AND status = 'valid' "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1", (episode_id,)
    ).fetchone()
    family = _loads(review["review_json"], {}).get("task_family") if review else None
    fixture = {
        "id": episode_id,
        "synthetic": False,
        "kind": "review",
        "task_family": clean.text(family) if isinstance(family, str) else None,
        "evidence": evidence,
        "episode": {
            "end_reason": ep["end_reason"],
            "duration_ms": ep["duration_ms"],
            "model": ep["model"],
            "completed_at": ep["completed_at"],
            "session": hash_id(ep["session_id"]),
            "repository": clean.value({k: repository.get(k) for k in _REPOSITORY_KEYS}),
            "loaded_harness": clean.value(_loads(ep["loaded_harness_json"], {})),
            "coverage": clean.value(_loads(ep["coverage_json"], {})),
        },
        "labels": {"labellers": [], "pass": None, "dimensions": {}, "adjudicated": False, "ambiguous": False},
        "probe": None,
        "export": {"format": FORMAT, "exported_at": iso(now), "sanitized": True},
    }
    report = {**clean.counts, "fields_dropped": dropped, "evidence_items": len(evidence)}
    return fixture, report


def write_export(path: str | Path, fixture: dict) -> Path:
    """Write the fixture to a new file (mode 0600); never overwrite."""
    target = Path(path)
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ExportError("output_exists", f"{target} already exists; choose a new file") from None
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(fixture, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")
    return target


def format_report(path: Path, report: dict) -> str:
    lines = [f"Wrote sanitized episode fixture {path} ({report['evidence_items']} evidence items). Removed:"]
    labels = (("secrets_redacted", "secret-shaped strings redacted"),
              ("session_ids_hashed", "session ids replaced by a hash"),
              ("repo_paths_relativized", "repository paths made relative"),
              ("paths_redacted", f"other absolute paths replaced by {REDACTED_PATH}"))
    for key, label in labels:
        lines.append(f"  {report[key]} {label}")
    dropped = ", ".join(report["fields_dropped"]) or "none"
    lines.append(f"  fields dropped: {dropped}")
    lines.append("Redaction is best effort: read the file before sharing it.")
    return "\n".join(lines)

