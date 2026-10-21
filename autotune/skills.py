"""Project skills: inventory, usage observations, overlap and merge validation (M6).

`inventory` reads `.claude/skills/**/SKILL.md` from a working tree, and
`inventory_at` reads the same from a Git commit. Each skill carries its
frontmatter name and description, body, support files (every other entry in
its directory, with size, executable bit, sha256 of the full content and a
kind: a regular file, a symlink or a gitlink) and the relative links its body
makes. A symlink's content is its link text and a gitlink's is its commit id;
neither is followed.

Usage is recorded as separate skill_events rows: `load` (the skill was in
context), `invocation` (the Skill tool was called for it) and `outcome` (the
review's outcome score). One is never inferred from another. Nothing here
prunes or deletes a skill by usage counts, and old skill bodies are not
archived: Git holds them, and `merge` rows link each removed source path to
the candidate that removed it.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .util import canonical_json, iso

SKILLS_DIR = ".claude/skills"
SKILL_FILE = "SKILL.md"
DEFAULT_OVERLAP_THRESHOLD = 0.45
LAST_SWEEP_META = "consolidate_last_sweep_at"
CONSOLIDATION_KIND = "consolidation"
TRIGGER_KEEP_FRACTION = 0.5
MAX_SKILL_BYTES = 512 * 1024

STOP_WORDS = frozenset(
    "a an and are as at be been but by can do does for from has have how i if in into is it its of on or "
    "our should so such that the their them then there these this those to use used uses using via was "
    "we were what when where which while who will with you your skill skills".split()
)
EXCEPTION_MARKERS = ("except", "unless", "do not", "don't", "never", "only when", "only if", "but not")

# Violation codes of validate_merge; each is also a candidate reason.
MERGE_VIOLATIONS = (
    "lost_exception",
    "lost_support_file",
    "executable_bit_changed",
    "broken_link",
    "protected_skill_removed",
    "trigger_lost",
)

_MD_LINK = re.compile(r"!?\[[^\]\n]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_BACKTICK = re.compile(r"`([^`\n]+)`")
_PATHLIKE = re.compile(r"^[A-Za-z0-9_.\-/]+$")
_WORD = re.compile(r"[a-z0-9]+")
_SENTENCE_END = re.compile(r"(?<=[.!?;])\s+")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")


SUPPORT_KINDS = ("file", "symlink", "gitlink")
_HASH_CHUNK = 1 << 20


@dataclass
class SupportFile:
    path: str           # relative to the skill directory
    size: int
    executable: bool
    sha256: str
    kind: str = "file"  # one of SUPPORT_KINDS

    def to_dict(self) -> dict:
        return {"path": self.path, "size": self.size, "executable": self.executable, "sha256": self.sha256,
                "kind": self.kind}


@dataclass
class _Entry:
    """One inventoried path: the bytes kept in memory (empty above
    MAX_SKILL_BYTES), and the size and sha256 of the full content."""
    data: bytes
    executable: bool
    size: int
    sha256: str
    kind: str = "file"


def _entry(data: bytes, executable: bool, kind: str = "file") -> _Entry:
    return _Entry(data if len(data) <= MAX_SKILL_BYTES else b"", executable, len(data),
                  hashlib.sha256(data).hexdigest(), kind)


@dataclass
class Link:
    target: str         # as written in the body
    kind: str           # "markdown" or "backtick"
    path: str | None    # normalized, relative to the skill directory; None when it leaves the directory
    resolves: bool

    def to_dict(self) -> dict:
        return {"target": self.target, "kind": self.kind, "path": self.path, "resolves": self.resolves}


@dataclass
class Skill:
    name: str
    description: str
    path: str                     # repository-relative skill directory
    body: str
    frontmatter: dict = field(default_factory=dict)
    support_files: list[SupportFile] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    pinned: bool = False
    critical: bool = False
    skill_sha256: str = ""
    skill_chars: int = 0

    @property
    def skill_file(self) -> str:
        return f"{self.path}/{SKILL_FILE}"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "path": self.path,
            "pinned": self.pinned,
            "critical": self.critical,
            "body": self.body,
            "support_files": [f.to_dict() for f in self.support_files],
            "links": [link.to_dict() for link in self.links],
        }


# Frontmatter


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split `---` frontmatter from the body. Supports `key: value` lines and
    `>` (folded) or `|` (literal) block values; other YAML is ignored."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            end = i
            break
    if end is None:
        return {}, text
    meta: dict = {}
    i = 1
    while i < end:
        line = lines[i]
        m = re.match(r"^([A-Za-z0-9_\-]+)\s*:\s*(.*)$", line)
        i += 1
        if not m or line[:1].isspace():
            continue
        key, value = m.group(1), m.group(2).strip()
        if value[:1] in (">", "|") and re.fullmatch(r"[>|][+-]?\d?", value):
            block = []
            while i < end and (not lines[i].strip() or lines[i][:1].isspace()):
                block.append(lines[i].strip())
                i += 1
            while block and not block[-1]:
                block.pop()
            if value[0] == "|":
                meta[key] = "\n".join(block)
            else:
                meta[key] = " ".join(b for b in block if b)
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        meta[key] = value
    body = "\n".join(lines[end + 1:])
    if text.endswith("\n"):
        body += "\n"
    return meta, body


# Inventory


def _rel_inside(target: str) -> str | None:
    """`target` normalized relative to the skill directory; None when it
    leaves the directory or names the directory itself."""
    if target.startswith("/") or "\\" in target or "\x00" in target:
        return None
    rel = posixpath.normpath(target)
    if rel in (".", "..") or rel.startswith("../"):
        return None
    return rel


def _clean_target(target: str) -> str | None:
    target = target.strip()
    if not target or target.startswith("#") or target.startswith("/") or _SCHEME.match(target):
        return None
    target = target.split("#", 1)[0].split("?", 1)[0]
    return target or None


def body_links(body: str, files: set[str], *, known: set[str] | frozenset = frozenset()) -> list[Link]:
    """Relative links in `body`. Markdown links count whenever they are
    relative. A backtick path counts when it names a file of the skill
    (`files`), a path in `known` (support files elsewhere in the comparison),
    or starts with `./` or `../`."""
    out: list[Link] = []
    seen: set[tuple[str, str]] = set()
    for m in _MD_LINK.finditer(body):
        target = _clean_target(m.group(1))
        if target is None:
            continue
        rel = _rel_inside(target)
        key = ("markdown", target)
        if key not in seen:
            seen.add(key)
            out.append(Link(target, "markdown", rel, rel is not None and rel in files))
    for m in _BACKTICK.finditer(body):
        raw = m.group(1).strip()
        if not _PATHLIKE.match(raw) or raw.startswith("-") or "/" not in raw and "." not in raw:
            continue
        rel = _rel_inside(raw)
        explicit = raw.startswith("./") or raw.startswith("../")
        if not explicit and (rel is None or (rel not in files and rel not in known)):
            continue
        key = ("backtick", raw)
        if key not in seen:
            seen.add(key)
            out.append(Link(raw, "backtick", rel, rel is not None and rel in files))
    return out


def _matches_any(globs, skill_dir: str) -> bool:
    for g in globs or []:
        if not isinstance(g, str) or not g:
            continue
        g = g.rstrip("/")
        if (config.glob_match(g, skill_dir) or config.glob_match(g, f"{skill_dir}/{SKILL_FILE}")
                or config.glob_match(g, f"{skill_dir}/x")):
            return True
    return False


def _build(skill_dir: str, files: dict[str, _Entry], cfg: dict | None) -> Skill | None:
    """`files` maps paths relative to the skill directory to entries."""
    raw = files.get(SKILL_FILE)
    if raw is None or raw.kind != "file":
        return None
    text = raw.data.decode("utf-8", errors="replace")
    meta, body = parse_frontmatter(text)
    cfg = cfg or {}
    support = [
        SupportFile(rel, e.size, bool(e.executable), e.sha256, e.kind)
        for rel, e in sorted(files.items()) if rel != SKILL_FILE
    ]
    names = {f.path for f in support}
    name = str(meta.get("name") or posixpath.basename(skill_dir))
    pinned = str(meta.get("autotune", "")).strip().lower() == "pinned" or _matches_any(cfg.get("pinned_skills"),
                                                                                         skill_dir)
    return Skill(
        name=name,
        description=str(meta.get("description") or ""),
        path=skill_dir,
        body=body,
        frontmatter=meta,
        support_files=support,
        links=body_links(body, names),
        pinned=pinned,
        critical=_matches_any(cfg.get("critical_skills"), skill_dir),
        skill_sha256=raw.sha256,
        skill_chars=len(text),
    )


def _group(entries: dict[str, _Entry]) -> list[tuple[str, dict]]:
    """Group repository paths under the skill directories (each directory
    holding a SKILL.md). A file belongs to its nearest skill directory."""
    dirs = sorted({posixpath.dirname(p) for p, e in entries.items()
                   if posixpath.basename(p) == SKILL_FILE and e.kind == "file"},
                  key=lambda d: (-d.count("/"), d))
    groups: dict[str, dict] = {d: {} for d in dirs}
    for path, value in entries.items():
        for d in dirs:
            if path.startswith(d + "/"):
                groups[d][path[len(d) + 1:]] = value
                break
    return sorted(groups.items())


def _file_entry(path: Path, st) -> _Entry:
    """A regular file, hashed in full but kept in memory only up to
    MAX_SKILL_BYTES."""
    exe = bool(st.st_mode & 0o111)
    if st.st_size <= MAX_SKILL_BYTES:
        return _entry(path.read_bytes(), exe)
    digest, size = hashlib.sha256(), 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_HASH_CHUNK), b""):
            digest.update(chunk)
            size += len(chunk)
    return _Entry(b"", exe, size, digest.hexdigest())


def _worktree_gitlinks(root: Path) -> dict[str, str]:
    """Gitlinks (submodules) under the skills directory, from the index of
    the Git work tree at `root` ({repo path: commit id}); {} outside Git."""
    from . import git_ops

    if not (root / ".git").exists():
        return {}
    try:
        proc = git_ops.run_git(root, ["ls-files", "-s", "-z", "--", SKILLS_DIR], check=False)
    except Exception:
        return {}
    if proc.returncode != 0:
        return {}
    out = {}
    for record in proc.stdout.decode("utf-8", "surrogateescape").split("\0"):
        if not record or "\t" not in record:
            continue
        meta, name = record.split("\t", 1)
        parts = meta.split(" ")
        if len(parts) >= 2 and parts[0] == "160000":
            out[name] = parts[1]
    return out


def inventory(root, cfg: dict | None = None) -> list[Skill]:
    """Skills in the working tree at `root`. Symlinks are listed as support
    entries of kind "symlink" (their link text is hashed; they are never
    followed) and submodules recorded in the index as kind "gitlink"."""
    root = Path(root)
    base = root / SKILLS_DIR
    entries: dict[str, _Entry] = {}
    if base.is_dir() and not base.is_symlink() and not (root / ".claude").is_symlink():
        gitlinks = {p: sha for p, sha in _worktree_gitlinks(root).items() if (root / p).is_dir()}
        for path, sha in gitlinks.items():
            entries[path] = _entry(sha.encode("ascii"), False, "gitlink")
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames.sort()
            names = list(filenames)
            keep = []
            for d in dirnames:
                rel = (Path(dirpath) / d).relative_to(root).as_posix()
                if (Path(dirpath) / d).is_symlink():
                    names.append(d)        # os.walk lists a symlinked directory here
                elif rel not in gitlinks:
                    keep.append(d)
            dirnames[:] = keep
            for name in sorted(names):
                path = Path(dirpath) / name
                try:
                    st = path.lstat()
                except OSError:
                    continue
                rel = path.relative_to(root).as_posix()
                if stat.S_ISLNK(st.st_mode):
                    try:
                        link = os.readlink(path)
                    except OSError:
                        continue
                    entries[rel] = _entry(os.fsencode(link), False, "symlink")
                elif stat.S_ISREG(st.st_mode):
                    entries[rel] = _file_entry(path, st)
    return [s for s in (_build(d, files, cfg) for d, files in _group(entries)) if s is not None]


def inventory_at(repo, rev: str, cfg: dict | None = None) -> list[Skill]:
    """Skills as committed at `rev` (read with git ls-tree and cat-file, never
    from the working tree)."""
    from . import git_ops

    proc = git_ops.run_git(repo, ["ls-tree", "-r", "-z", "--full-tree", rev, "--", SKILLS_DIR], check=False)
    if proc.returncode != 0:
        git_ops.rev_parse(repo, rev)
        return []
    entries: dict[str, _Entry] = {}
    for record in proc.stdout.decode("utf-8", "surrogateescape").split("\0"):
        if not record:
            continue
        meta, name = record.split("\t", 1)
        mode, otype, sha = meta.split(" ")
        if otype == "commit" and mode == "160000":
            entries[name] = _entry(sha.encode("ascii"), False, "gitlink")
            continue
        if otype != "blob" or mode not in ("100644", "100755", "120000"):
            continue
        data = git_ops.run_git(repo, ["cat-file", "blob", sha]).stdout
        entries[name] = _entry(data, mode == "100755", "symlink" if mode == "120000" else "file")
    return [s for s in (_build(d, files, cfg) for d, files in _group(entries)) if s is not None]


def by_path(skills: list[Skill]) -> dict[str, Skill]:
    return {s.path: s for s in skills}


def token_estimate(text: str | bytes | None) -> int:
    """Characters / 4, rounded up."""
    if text is None:
        return 0
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    return (len(text) + 3) // 4


def skill_tokens_at(repo, rev: str, skill_dirs: list[str]) -> int:
    """Token estimate of the SKILL.md files of `skill_dirs` at `rev` (a removed
    skill counts zero)."""
    from . import git_ops

    return sum(token_estimate(git_ops.file_at(repo, rev, f"{d}/{SKILL_FILE}")) for d in skill_dirs)


# Items and overlap


def normalize_text(text: str) -> str:
    return " ".join(str(text).split()).casefold()


def _content_words(text: str) -> list[str]:
    return [w for w in _WORD.findall(str(text).casefold()) if w not in STOP_WORDS]


def _sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENTENCE_END.split(" ".join(str(text).split()))]
    return [p for p in parts if p]


def _strip_bullet(line: str) -> str:
    return re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", line).strip()


def extract_items(skill: Skill) -> dict:
    """Mechanical items a merge must keep: triggers (description sentences),
    exceptions (body lines containing an exception marker) and support files."""
    exceptions: list[str] = []
    for line in skill.body.splitlines():
        text = _strip_bullet(line)
        if not text or text.startswith("#"):
            continue
        low = text.casefold()
        if any(re.search(r"(?<![a-z])" + re.escape(m) + r"(?![a-z])", low) for m in EXCEPTION_MARKERS):
            if text not in exceptions:
                exceptions.append(text)
    return {
        "triggers": _sentences(skill.description),
        "exceptions": exceptions,
        "support_files": [f.path for f in skill.support_files],
    }


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def trigger_phrases(skill: Skill) -> set[str]:
    """Adjacent content-word pairs of each trigger sentence."""
    out: set[str] = set()
    for sentence in _sentences(skill.description):
        words = _content_words(sentence)
        out.update(f"{x} {y}" for x, y in zip(words, words[1:]))
        if len(words) == 1:
            out.add(words[0])
    return out


def overlap_score(a: Skill, b: Skill) -> tuple[float, list[str]]:
    wa, wb = set(_content_words(a.description)), set(_content_words(b.description))
    ta, tb = trigger_phrases(a), trigger_phrases(b)
    words, phrases = _jaccard(wa, wb), _jaccard(ta, tb)
    reasons = [f"description words jaccard {words:.2f}", f"trigger phrases jaccard {phrases:.2f}"]
    shared = sorted(wa & wb)
    if shared:
        reasons.append("shared words: " + ", ".join(shared[:12]))
    return max(words, phrases), reasons


def _protected(skill: Skill) -> bool:
    return skill.pinned or skill.critical


def overlaps(skills: list[Skill], threshold: float = DEFAULT_OVERLAP_THRESHOLD) -> list[tuple]:
    """Pairs (a_path, b_path, score, reasons) scoring at least `threshold`,
    best first. A pair where neither skill could be a merge source (both
    pinned or critical) is never proposed."""
    out = []
    ordered = sorted(skills, key=lambda s: s.path)
    for i, a in enumerate(ordered):
        for b in ordered[i + 1:]:
            if _protected(a) and _protected(b):
                continue
            score, reasons = overlap_score(a, b)
            if score >= threshold and score > 0:
                out.append((a.path, b.path, round(score, 4), reasons))
    out.sort(key=lambda t: (-t[2], t[0], t[1]))
    return out


def threshold(cfg: dict | None) -> float:
    value = (cfg or {}).get("consolidation_overlap_threshold", DEFAULT_OVERLAP_THRESHOLD)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        return DEFAULT_OVERLAP_THRESHOLD
    return float(value)


# Coverage and merge validation


def coverage_missing(sources: list[Skill], coverage_map: list) -> list[dict]:
    """Exceptions and support files of `sources` with no coverage_map entry
    (with a non-empty destination). Files match exactly; text matches after
    whitespace and case normalization."""
    covered_files: set[tuple[str, str]] = set()
    covered_text: set[tuple[str, str]] = set()
    for entry in coverage_map or []:
        if not isinstance(entry, dict) or not str(entry.get("destination") or "").strip():
            continue
        src = str(entry.get("source") or "").rstrip("/")
        item = str(entry.get("item") or "")
        if entry.get("kind") == "support_file":
            covered_files.add((src, item))
        else:
            covered_text.add((src, normalize_text(item)))
    missing = []
    for skill in sources:
        items = extract_items(skill)
        for text in items["exceptions"]:
            if (skill.path, normalize_text(text)) not in covered_text:
                missing.append({"source": skill.path, "kind": "exception", "item": text})
        for rel in items["support_files"]:
            if (skill.path, rel) not in covered_files and (skill.path, f"{skill.path}/{rel}") not in covered_files:
                missing.append({"source": skill.path, "kind": "support_file", "item": rel})
    return missing


def support_destination(target: str, source_path: str, rel: str, coverage_map: list) -> str:
    """Path under the target directory where one support file of
    `source_path` lives after the merge: the destination of its support_file
    coverage entry when that names a safe path inside the target, else the
    same relative path. Only support_file entries count. consolidate moves
    files here and validate_merge looks for them here."""
    for entry in coverage_map or []:
        if not isinstance(entry, dict) or entry.get("kind") != "support_file":
            continue
        if str(entry.get("source") or "").rstrip("/") != source_path:
            continue
        if entry.get("item") not in (rel, f"{source_path}/{rel}"):
            continue
        dest = str(entry.get("destination") or "").strip()
        if dest.startswith(target + "/"):
            dest = dest[len(target) + 1:]
        clean = _rel_inside(dest) if dest else None
        if clean and clean != SKILL_FILE and not clean.startswith(".claude/"):
            return clean
    return rel


def _violation(code: str, path: str, detail: str) -> dict:
    return {"code": code, "path": path, "detail": detail[:300]}


def validate_merge(base_inventory: list[Skill], new_inventory: list[Skill], coverage_map: list, *,
                   target: str, sources: list[str]) -> list[dict]:
    """Violations of a merge of `sources` into `target`, comparing the skills
    at the base with the skills after the edit. Empty when the merge keeps
    every exception, support file, executable bit, link and trigger.

    Every support file of the target and the sources must be in the target
    with the same sha256 (its path may change) and executable bit; the
    coverage map only says where a moved file goes (support_destination),
    never excuses a missing or changed one. A symlink or gitlink must be at
    its destination unchanged. Every trigger of the target and the sources
    must be kept, for a merge and for an update."""
    before = by_path(base_inventory)
    after = by_path(new_inventory)
    out: list[dict] = []
    involved = [p for p in [target, *sources] if p in before]
    new_target = after.get(target)
    if new_target is None:
        out.append(_violation("protected_skill_removed" if _protected(before.get(target) or _blank(target))
                              else "lost_support_file", target, "the merge target no longer exists"))
        return out

    # Removed skill directories must be declared, unprotected sources.
    for path, skill in before.items():
        if path in after:
            continue
        if path not in sources or _protected(skill):
            out.append(_violation("protected_skill_removed", path,
                                  "pinned or critical skill removed" if _protected(skill)
                                  else "skill removed but not a declared source"))

    target_body = normalize_text(new_target.body)
    target_files = {f.path: f for f in new_target.support_files}
    by_sha: dict[tuple[str, str], list] = {}
    for f in new_target.support_files:
        by_sha.setdefault((f.kind, f.sha256), []).append(f)

    for path in involved:
        old = before[path]
        for text in extract_items(old)["exceptions"]:
            if normalize_text(text) not in target_body:
                out.append(_violation("lost_exception", path, text))
        for f in old.support_files:
            where = f"{path}/{f.path}"
            dest = f.path if path == target else support_destination(target, path, f.path, coverage_map)
            if f.kind != "file":
                # A symlink or gitlink is never moved or rewritten: the target
                # must hold the identical entry at its destination.
                g = target_files.get(dest)
                if g is None or g.kind != f.kind or g.sha256 != f.sha256:
                    out.append(_violation("lost_support_file", where,
                                          f"{f.kind} not present unchanged at {target}/{dest}"))
                continue
            same = by_sha.get(("file", f.sha256), [])
            if same:
                if all(g.executable != f.executable for g in same):
                    out.append(_violation("executable_bit_changed", where,
                                          f"was {'executable' if f.executable else 'not executable'}"))
                continue
            # Support files are moved, never rewritten: a changed, emptied
            # or missing file is lost, whatever the coverage map says.
            g = target_files.get(dest)
            state = "missing" if g is None or g.kind != "file" else "content changed"
            out.append(_violation("lost_support_file", where,
                                  f"no file with the same content in the target ({target}/{dest}: {state})"))

    known = set()
    for path in involved:
        for f in before[path].support_files:
            known.add(f.path)
    for link in body_links(new_target.body, set(target_files), known=known):
        if not link.resolves:
            out.append(_violation("broken_link", f"{target}/{SKILL_FILE}", link.target))

    new_description = normalize_text(new_target.description)
    new_words = set(_content_words(new_target.description))
    # The target's own triggers count as well as each source's, for a merge
    # and for an update (which has no sources).
    for path in involved:
        triggers = extract_items(before[path])["triggers"]
        if triggers and not any(_trigger_kept(t, new_description, new_words) for t in triggers):
            out.append(_violation("trigger_lost", path, triggers[0]))
    return _dedupe(out)


def _blank(path: str) -> Skill:
    return Skill(name=posixpath.basename(path), description="", path=path, body="")


def _trigger_kept(trigger: str, description: str, words: set[str]) -> bool:
    if normalize_text(trigger) in description:
        return True
    key = _content_words(trigger)
    if not key:
        return False
    return sum(1 for w in key if w in words) >= TRIGGER_KEEP_FRACTION * len(key)


def _dedupe(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for v in items:
        key = (v["code"], v["path"], v["detail"])
        if key not in seen:
            seen.add(key)
            out.append(v)
    return out


# Usage observations


def _loads(text, default):
    if text is None:
        return default
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def _invoked_skill(content: str | None) -> str | None:
    """The skill name from a Skill tool call's input summary."""
    if not content:
        return None
    text = content.strip()
    obj = _loads(text, None) if text.startswith("{") else None
    if isinstance(obj, dict):
        for key in ("skill", "name", "command"):
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:200]
        return None
    m = re.search(r'"?(?:skill|name|command)"?\s*[:=]\s*"?([A-Za-z0-9_.:/\-]+)', text)
    if m:
        return m.group(1)[:200]
    first = text.split()[0] if text.split() else ""
    return first[:200] if re.fullmatch(r"[A-Za-z0-9_.:/\-]+", first) else None


def episode_skills(conn, episode_id: str) -> tuple[list[dict], list[str]]:
    """(loaded skills [{skill, source, path}], invoked skill names) from the
    episode's unexpired evidence."""
    loads, invoked = [], []
    rows = conn.execute(
        "SELECT kind, content, provenance_json FROM evidence WHERE episode_id = ? AND expired_at IS NULL "
        "AND kind IN ('skill_load', 'tool_call') ORDER BY position",
        (episode_id,),
    ).fetchall()
    for r in rows:
        if r["kind"] == "skill_load":
            obj = _loads(r["content"], {})
            if isinstance(obj.get("skill"), str) and obj["skill"]:
                loads.append({"skill": obj["skill"], "source": obj.get("source"), "path": obj.get("path")})
        elif _loads(r["provenance_json"], {}).get("tool") == "Skill":
            name = _invoked_skill(r["content"])
            if name:
                invoked.append(name)
    return loads, invoked


def record_episode(conn, episode_id: str, now) -> int:
    """Insert load, invocation and outcome skill_events for one episode.

    Runs in the caller's transaction (the review commit). Rerunning replaces
    the episode's earlier rows, so a second review does not double count.
    Returns the number of rows written."""
    conn.execute(
        "DELETE FROM skill_events WHERE episode_id = ? AND event IN ('load', 'invocation', 'outcome')",
        (episode_id,),
    )
    loads, invoked = episode_skills(conn, episode_id)
    ts = iso(now)
    n = 0
    for item in loads:
        conn.execute(
            "INSERT INTO skill_events (episode_id, skill, source, path, event, detail_json, occurred_at) "
            "VALUES (?, ?, ?, ?, 'load', '{}', ?)",
            (episode_id, item["skill"], item.get("source"), item.get("path"), ts),
        )
        n += 1
    for name in invoked:
        conn.execute(
            "INSERT INTO skill_events (episode_id, skill, source, event, detail_json, occurred_at) "
            "VALUES (?, ?, 'tool.call', 'invocation', '{}', ?)",
            (episode_id, name, ts),
        )
        n += 1
    review = conn.execute(
        "SELECT review_id, review_json, recommendation FROM reviews WHERE episode_id = ? AND status = 'valid' "
        "ORDER BY created_at DESC LIMIT 1",
        (episode_id,),
    ).fetchone()
    if review is not None:
        obj = _loads(review["review_json"], {})
        score = ((obj.get("dimensions") or {}).get("outcome") or {}).get("score")
        detail = canonical_json({"review_id": review["review_id"], "outcome_score": score,
                                 "recommendation": review["recommendation"]})
        names = list(dict.fromkeys([i["skill"] for i in loads] + invoked))
        for name in names:
            conn.execute(
                "INSERT INTO skill_events (episode_id, skill, source, event, detail_json, occurred_at) "
                "VALUES (?, ?, 'review', 'outcome', ?, ?)",
                (episode_id, name, detail, ts),
            )
            n += 1
    return n


def record_merge(conn, candidate_row, now) -> int:
    """Insert one `merge` skill_events row per source skill the applied
    consolidation removed or merged, linked to the candidate. Runs in the
    caller's transaction (the apply commit); idempotent per candidate."""
    cand = dict(candidate_row)
    if cand.get("change_kind") != CONSOLIDATION_KIND:
        return 0
    cid = cand["candidate_id"]
    if conn.execute("SELECT 1 FROM skill_events WHERE candidate_id = ? AND event = 'merge' LIMIT 1",
                    (cid,)).fetchone():
        return 0
    info = consolidation_info(cand)
    target = info.get("target")
    n = 0
    for source in info.get("sources") or []:
        detail = canonical_json({"target": target, "applied_commit": cand.get("applied_commit"),
                                 "candidate_commit": cand.get("candidate_commit")})
        conn.execute(
            "INSERT INTO skill_events (skill, source, path, event, candidate_id, detail_json, occurred_at) "
            "VALUES (?, 'autotune', ?, 'merge', ?, ?, ?)",
            (posixpath.basename(source), source, cid, detail, iso(now)),
        )
        n += 1
    return n


def consolidation_info(candidate_row: dict) -> dict:
    """{"target", "sources", "skills", "coverage_map"} stored with a
    consolidation candidate (in validation_json), or {} when absent."""
    validation = _loads(dict(candidate_row).get("validation_json"), {})
    info = validation.get("consolidation") if isinstance(validation, dict) else None
    return info if isinstance(info, dict) else {}


def candidate_skill_dirs(candidate_row: dict) -> list[str]:
    """The skill directories a consolidation candidate may change."""
    from . import cases

    return cases.consolidation_dirs(dict(candidate_row))
