#!/usr/bin/env python3
"""Inventory a markdown / Obsidian vault before refactoring. Read-only.

Usage:
    python scripts/inventory.py <root> [--json] [--top N] [--max-files N]

What it reports:

  * file totals and extension groups (md / image / pdf / code / config / other)
  * per-directory file and md counts, root-level md ("杂乱第一现场")
  * one summary line per md file: lines, bytes, mtime, frontmatter, first H1,
    `##` count, `## N.` split units, outbound wikilinks, embeds, table, code
    block, BOM, CRLF
  * split candidates ranked by `## N.` count (one section = one note); a file
    under 40 lines, or whose every section has under 8 body lines, is listed
    as too short to split and kept out of the main list
  * suspected duplicates: same basename, same normalised first 2 KB, same
    byte size + same title
  * dead-link pre-scan using the same case-sensitive resolution as
    validate_vault.py; case-only mismatches (`[[guide]]` vs `Guide.md`) are
    reported separately, and links into a source tree are reported as
    "code anchors" (never dead links). Links inside `%%...%%` comments,
    `<!-- ... -->` comments and indented code blocks are not links.
  * attachment inventory, orphan attachments, duplicate attachments
  * project kind: notes-vault / code-repo / docs-site / mixed, plus paths that
    a build system references (move-blocked candidates)
  * hygiene: BOM, CRLF, empty files, spaces in names, paths deeper than 6,
    ʻ未命名/副本/copy` names, .obsidian attachment folder setting
  * 3-5 data-driven next-step recommendations

The script never writes anything. It uses only the Python 3 standard library
and runs on Windows and Linux. Deterministic: no timestamps in the output.

Exit codes: 0 ok, 2 path missing / not a directory / bad arguments.
"""

import argparse
import datetime as _dt
import hashlib
import json
import os
import posixpath
import re
import stat
import sys
import unicodedata
import urllib.parse
from pathlib import Path

VERSION = "1.0"

DEFAULT_TOP = 10
DEFAULT_MAX_FILES = 5000
DEEP_DEPTH = 6
MAX_ANALYZE_BYTES = 8 * 1024 * 1024
TEXT_SCAN_BYTES = 2 * 1024 * 1024
# SKILL.md: a subsection needs at least 8 body lines to be worth its own note,
# and a file shorter than 40 lines is not worth splitting at all.
MIN_SECTION_BODY_LINES = 8
MIN_SPLIT_FILE_LINES = 40
BOM = b"\xef\xbb\xbf"

# Directories that are never part of the note inventory. `.obsidian` is read
# explicitly for app.json but otherwise skipped so plugin blobs do not swamp
# the counts.
SKIP_DIRS = {
    ".git", ".svn", ".hg", ".obsidian", ".trash",
    "node_modules", "__pycache__", ".venv", "venv", ".tox",
    "dist", "build", "target", ".next", ".nuxt", "coverage",
    ".cache", ".idea", ".vscode", ".gradle", "vendor",
}
SKIP_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}

MD_EXTS = {".md", ".markdown"}
IMAGE_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".ico",
    ".tif", ".tiff", ".avif", ".heic",
}
PDF_EXTS = {".pdf"}
CODE_EXTS = {
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".java", ".kt",
    ".kts", ".scala", ".groovy", ".go", ".rs", ".c", ".h", ".cc", ".cpp",
    ".hpp", ".cs", ".rb", ".php", ".swift", ".m", ".mm", ".sh", ".ps1",
    ".bat", ".sql", ".vue", ".svelte", ".lua", ".r", ".dart", ".ex",
    ".exs", ".pl", ".proto", ".ino", ".asm", ".clj", ".fs",
}
CONFIG_EXTS = {
    ".json", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".env",
    ".properties", ".xml", ".lock", ".gradle", ".cmake", ".mk",
}
TEXT_EXTS = {"txt", "yml", "yaml", "csv", "tsv", "toml"}
STRUCTURAL_NAMES = {
    "license", "licence", "makefile", "dockerfile", "procfile", "gemfile",
    "rakefile", "changelog", "contributing", "codeowners",
}

EXT_GROUP = {}
for _group, _exts in (
    ("md", MD_EXTS),
    ("image", IMAGE_EXTS),
    ("pdf", PDF_EXTS),
    ("code", CODE_EXTS),
    ("config", CONFIG_EXTS),
):
    for _ext in _exts:
        EXT_GROUP[_ext.lstrip(".")] = _group

GROUP_ORDER = ["md", "image", "pdf", "code", "config", "other"]
GROUP_LABELS = {
    "md": "md", "image": "图片", "pdf": "PDF",
    "code": "代码", "config": "配置", "other": "其它",
}

FENCE_OPEN_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})(.*)$")
FENCE_CLOSE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})[ \t]*$")
FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", re.S)
H1_RE = re.compile(r"^#\s+(.+?)\s*#*\s*$", re.M)
H2_RE = re.compile(r"^##[ \t]+\S", re.M)
SPLIT_RE = re.compile(r"^##[ \t]+\d+[.、]", re.M)
WIKI_RE = re.compile(r"(!?)\[\[([^\[\]\n]+?)\]\]")
MD_LINK_RE = re.compile(r"(!?)\[([^\]\n]*)\]\(\s*<?([^)\s>]+)>?")
CODE_FENCE_RE = re.compile(r"^[ \t]*(```|~~~)", re.M)
INDENT_CODE_RE = re.compile(r"^(?: {4}|\t)\S", re.M)
# Text that only looks like a link: Obsidian comments, HTML comments and
# indented code blocks. They are blanked before links are scanned.
OBSIDIAN_COMMENT_RE = re.compile(r"%%.*?%%|<!--.*?-->", re.S)
# 行内代码里的 [[X]] 不是链接（与 validate_vault.py 同一口径）
INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
INDENT_CODE_START_RE = re.compile(r"^(?: {4,}|\t)")
LIST_MARKER_RE = re.compile(r"^(?:[-*+][ \t]|\d+[.)][ \t])")
HEADING_LINE_RE = re.compile(r"^#{1,6}(?:[ \t]|$)")
TABLE_SEP_RE = re.compile(r"^\|?[ \t]*:?-{2,}")
WS_RE = re.compile(r"\s+")

# Reused from scripts/validate_vault.py so both tools agree on what counts as
# an anchor into a source tree rather than a dead vault link.
CODE_PATH_HINT = re.compile(
    r"(^|/)\.\.(/|$)"
    r"|(^|/)(src|lib|app|web|frontend|backend|gradle|maven|target"
    r"|node_modules|vendor|packages|cmd|internal)(/|$)"
    r"|\.(java|kt|kts|scala|groovy|xml|yml|yaml|properties|gradle|sql"
    r"|ts|tsx|js|jsx|vue|svelte|py|go|rs|proto|sh|ps1|bat|cs|cpp|cc|c"
    r"|h|hpp|rb|php|swift)(?::\d+(?:-\d+)?)?$",
    re.I,
)

SUSPICIOUS_COPY = ("副本", "copy")


class FileInfo:
    __slots__ = ("path", "rel", "name", "ext", "size", "mtime", "depth", "is_md", "is_text", "group")

    def __init__(self, path, rel, name, size, mtime):
        self.path = path
        self.rel = rel
        self.name = name
        self.ext = ext_of(name)
        self.size = size
        self.mtime = mtime
        self.depth = rel.count("/") + 1
        self.is_md = ("." + self.ext) in MD_EXTS
        self.is_text = self.is_md or self.ext in TEXT_EXTS
        self.group = group_of(name, self.ext)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def ext_of(name):
    suffix = posixpath.splitext(name)[1]
    if not suffix or suffix == ".":
        return "noext"
    return suffix.lower().lstrip(".")


def group_of(name, ext):
    group = EXT_GROUP.get(ext)
    if group:
        return group
    if ext == "noext" and name.startswith("."):
        return "config"
    return "other"


def is_md_rel(rel):
    return posixpath.splitext(rel)[1].lower() in MD_EXTS


def is_attachment(info):
    """A media/document attachment, not a config or source file.

    Build-system files (`mkdocs.yml`, `package.json`, ...) are non-md files too,
    but calling them "orphan attachments" would be noise. They are still
    counted in the non-md inventory; only orphan/duplicate detection skips them.
    """
    if info.is_md:
        return False
    if info.group in ("code", "config"):
        return False
    if info.name.startswith("."):
        return False
    if info.name.casefold() in STRUCTURAL_NAMES:
        return False
    return True


def human_size(num):
    value = float(num)
    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024.0
        idx += 1
    if idx == 0:
        return "%d B" % int(value)
    return "%.1f %s" % (value, units[idx])


def dwidth(text):
    total = 0
    for ch in text:
        total += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return total


def fit(text, width):
    text = str(text)
    if dwidth(text) <= width:
        return text
    out = []
    used = 0
    for ch in text:
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + cw > width - 1:
            break
        out.append(ch)
        used += cw
    return "".join(out) + "…"


def fit_tail(text, width):
    text = str(text)
    if dwidth(text) <= width:
        return text
    out = []
    used = 0
    for ch in reversed(text):
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + cw > width - 1:
            break
        out.append(ch)
        used += cw
    return "…" + "".join(reversed(out))


def pad(text, width):
    text = str(text)
    return text + " " * max(0, width - dwidth(text))


def render_table(headers, rows, widths):
    lines = []
    lines.append("  ".join(pad(headers[i], widths[i]) for i in range(len(headers))).rstrip())
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        lines.append("  ".join(pad(row[i], widths[i]) for i in range(len(headers))).rstrip())
    return lines


def join_rel(dir_rel, name):
    return name if dir_rel in ("", ".") else dir_rel + "/" + name


def read_bytes(path, limit):
    with open(path, "rb") as handle:
        return handle.read(limit)


def read_text_file(path, limit=TEXT_SCAN_BYTES):
    return read_bytes(path, limit).decode("utf-8-sig", errors="replace")


def hash_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def strip_fences(text):
    """Blank fenced code blocks, keeping line count; return (text, unclosed).

    Mirrors scripts/validate_vault.py: ``` and ~~~ fences, indented fences,
    longer closing runs, and an unclosed fence swallows the rest of the file.
    """
    out = []
    fence_char = None
    fence_len = 0
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        keyword = "\n" if line.endswith(("\n", "\r")) else ""
        if fence_char is None:
            match = FENCE_OPEN_RE.match(stripped)
            # An info string on a backtick fence must not contain a backtick.
            if match and not (match.group(1)[0] == "`" and "`" in match.group(2)):
                fence_char = match.group(1)[0]
                fence_len = len(match.group(1))
                out.append(keyword)
                continue
            out.append(line)
            continue
        match = FENCE_CLOSE_RE.match(stripped)
        if match and match.group(1)[0] == fence_char and len(match.group(1)) >= fence_len:
            fence_char = None
            fence_len = 0
        out.append(keyword)
    return "".join(out), fence_char is not None


def blank_keep_lines(text):
    """Replace every non-newline character with a space, keeping line count."""
    return "".join(
        "\n" if ch == "\n" else ("\r" if ch == "\r" else " ")
        for ch in text
    )


def strip_comments(text):
    """Blank `%%...%%` / `<!-- ... -->` comments and inline code spans.

    Blanking (instead of deleting) keeps every link on its original line, and
    a `[[...]]` inside a comment or inline code no longer looks like a link:
    notes are written for humans, nobody should rewrite them to clear warnings.
    """
    text = INLINE_CODE_RE.sub(lambda match: blank_keep_lines(match.group(0)), text)
    return OBSIDIAN_COMMENT_RE.sub(lambda match: blank_keep_lines(match.group(0)), text)


def strip_indented_code(text):
    """Blank indented code blocks (>=4 spaces or a tab), keeping line count.

    A block only starts on an indented line that follows a blank line and whose
    content is not a list item, blockquote or table row; it ends at the next
    blank line. The guard is deliberately conservative so links in nested
    lists (e.g. `    - [[x]]`) are never stripped.
    """
    out = []
    in_code = False
    prev_blank = True
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        ending = line[len(body):]
        content = body.lstrip(" \t")
        if in_code:
            if content:
                out.append(" " * len(body) + ending)
                prev_blank = False
            else:
                in_code = False
                out.append(line)
                prev_blank = True
            continue
        if not content:
            out.append(line)
            prev_blank = True
            continue
        if (
            prev_blank
            and INDENT_CODE_START_RE.match(body)
            and not (
                LIST_MARKER_RE.match(content)
                or content.startswith(">")
                or content.startswith("|")
            )
        ):
            in_code = True
            out.append(" " * len(body) + ending)
            prev_blank = False
            continue
        out.append(line)
        prev_blank = False
    return "".join(out)


def strip_link_noise(text):
    """Remove everything that looks like a link but is not one."""
    return strip_indented_code(strip_comments(text))


def parse_frontmatter_fields(text):
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}
    fields = {}
    for line in match.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip().strip('"').strip("'")
    return fields


def clean_wiki_target(raw):
    target = raw.split("|", 1)[0].split("#", 1)[0]
    target = target.rstrip("\\").strip()
    return target


def looks_like_code(target):
    return bool(CODE_PATH_HINT.search(str(target)))


def has_table(text):
    lines = text.splitlines()
    for idx in range(len(lines) - 1):
        if lines[idx].count("|") >= 2 and TABLE_SEP_RE.match(lines[idx + 1].strip()):
            return True
    return False


def detect_bom_crlf(path, limit):
    bom = False
    crlf = False
    prev_cr = False
    total = 0
    with open(path, "rb") as handle:
        first = True
        while total < limit:
            chunk = handle.read(min(65536, limit - total))
            if not chunk:
                break
            if first:
                bom = chunk.startswith(BOM)
                first = False
            if b"\r\n" in chunk or (prev_cr and chunk[:1] == b"\n"):
                crlf = True
            prev_cr = chunk.endswith(b"\r")
            total += len(chunk)
    return bom, crlf


def even_sample(items, count):
    if count <= 0 or not items:
        return []
    if count >= len(items):
        return list(items)
    if count == 1:
        return [items[0]]
    step = len(items) / float(count)
    indexes = sorted({int(i * step) for i in range(count)})
    return [items[i] for i in indexes]


# ---------------------------------------------------------------------------
# enumeration
# ---------------------------------------------------------------------------

def enumerate_files(root):
    files = []
    dir_info = {}

    def dir_entry(rel_dir):
        return dir_info.setdefault(
            rel_dir,
            {"path": rel_dir, "files": 0, "md": 0, "has_subdirs": False},
        )

    for dirpath, dirnames, filenames in os.walk(str(root), onerror=lambda err: None):
        dirnames[:] = sorted(
            name for name in dirnames
            if name not in SKIP_DIRS and not os.path.islink(os.path.join(dirpath, name))
        )
        rel_dir = os.path.relpath(dirpath, str(root)).replace(os.sep, "/")
        entry = dir_entry(rel_dir)
        entry["has_subdirs"] = entry["has_subdirs"] or bool(dirnames)
        for sub in dirnames:
            dir_entry(join_rel(rel_dir, sub))

        for name in sorted(filenames):
            if name in SKIP_FILES:
                continue
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                continue
            try:
                st = os.stat(full)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            rel = join_rel(rel_dir, name)
            info = FileInfo(Path(full), rel, name, st.st_size, st.st_mtime)
            files.append(info)
            entry["files"] += 1
            if info.is_md:
                entry["md"] += 1

    dirs = sorted(
        dir_info.values(),
        key=lambda item: (item["path"] != ".", item["path"]),
    )
    return files, dirs


def build_indexes(files, root):
    """Build case-sensitive indexes, plus casefolded ones for diagnostics.

    The case-sensitive tables must mirror scripts/validate_vault.py, which keys
    its indexes by the literal `p.stem` / relative path. A casefolded lookup
    here would resolve [[guide]] to Guide.md and hide a link the validator
    reports as broken-wikilink.
    """
    rel_path = {}
    stem_index = {}
    name_index = {}
    fold_rel = {}
    fold_stem = {}
    fold_name = {}
    for info in files:
        rel_path[info.rel] = info.rel
        fold_rel.setdefault(info.rel.casefold(), []).append(info.rel)
        if info.is_md:
            stem = Path(info.name).stem
            stem_index.setdefault(stem, []).append(info.rel)
            fold_stem.setdefault(stem.casefold(), []).append(info.rel)
        name_index.setdefault(info.name, []).append(info.rel)
        fold_name.setdefault(info.name.casefold(), []).append(info.rel)
    for table in (stem_index, name_index, fold_rel, fold_stem, fold_name):
        for key in table:
            table[key].sort()
    return {
        "root": root,
        "rel_path": rel_path,
        "stem_index": stem_index,
        "name_index": name_index,
        "fold_rel": fold_rel,
        "fold_stem": fold_stem,
        "fold_name": fold_name,
    }


def resolve_wiki(target, md_rel, ctx):
    target = target.replace("\\", "/").strip().strip("/")
    if not target:
        return None
    target = urllib.parse.unquote(target)
    suffix = posixpath.splitext(target)[1].lower()
    variants = [target]
    if not suffix:
        variants.append(target + ".md")
    base_dir = posixpath.dirname(md_rel)
    for variant in variants:
        if variant in ctx["rel_path"]:
            return ctx["rel_path"][variant]
        joined = posixpath.normpath(posixpath.join(base_dir, variant))
        if joined in ctx["rel_path"]:
            return ctx["rel_path"][joined]
    base = posixpath.basename(target)
    if not suffix:
        matches = ctx["stem_index"].get(base)
        if matches:
            return matches[0]
    matches = ctx["name_index"].get(base)
    if matches:
        return matches[0]
    return None


def resolve_wiki_casefold(target, md_rel, ctx):
    """Return one existing file that matches only when case is ignored.

    Used after resolve_wiki() failed: if ignoring case yields exactly one file,
    the link is a case mismatch (broken for the validator, openable on Windows)
    rather than a dead link. Ambiguous matches return None on purpose.
    """
    target = target.replace("\\", "/").strip().strip("/")
    if not target:
        return None
    target = urllib.parse.unquote(target)
    suffix = posixpath.splitext(target)[1].lower()
    variants = [target]
    if not suffix:
        variants.append(target + ".md")
    base_dir = posixpath.dirname(md_rel)
    hits = []
    for variant in variants:
        hits.extend(ctx["fold_rel"].get(variant.casefold(), ()))
        joined = posixpath.normpath(posixpath.join(base_dir, variant))
        hits.extend(ctx["fold_rel"].get(joined.casefold(), ()))
    if not hits:
        base = posixpath.basename(target)
        if not suffix:
            hits.extend(ctx["fold_stem"].get(base.casefold(), ()))
        if not hits:
            hits.extend(ctx["fold_name"].get(base.casefold(), ()))
    unique = sorted(set(hits))
    if len(unique) == 1:
        return unique[0]
    return None


def resolve_relative(root, md_rel, raw):
    target = raw.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1]
    target = target.replace("\\", "/").strip()
    if not target or target.startswith("#"):
        return None
    if re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*:", target):
        return None
    if "{{" in target:
        return None
    target = urllib.parse.unquote(target)
    target = target.split("#", 1)[0].split("?", 1)[0].strip()
    if not target:
        return None
    base_dir = posixpath.dirname(md_rel)
    candidate = posixpath.normpath(posixpath.join(base_dir, target))
    inside = not (
        candidate == ".." or candidate.startswith("../") or posixpath.isabs(candidate)
    )
    exists = False
    if inside:
        try:
            exists = (root / candidate).exists()
        except OSError:
            exists = False
    return candidate, inside, exists


# ---------------------------------------------------------------------------
# markdown analysis
# ---------------------------------------------------------------------------

def scan_links(text, md_rel, ctx):
    dead = []
    case_mismatches = []
    anchors = set()
    refs = set()
    seen = set()
    seen_case = set()

    def add_dead(target, kind):
        key = (md_rel, target, kind)
        if key in seen:
            return
        seen.add(key)
        dead.append({"path": md_rel, "target": target, "kind": kind})

    def add_case_mismatch(target, actual):
        key = (md_rel, target, actual)
        if key in seen_case:
            return
        seen_case.add(key)
        case_mismatches.append({
            "path": md_rel, "target": target, "actual": actual,
        })

    for match in WIKI_RE.finditer(text):
        is_embed = bool(match.group(1))
        target = clean_wiki_target(match.group(2))
        if not target:
            continue
        resolved = resolve_wiki(target, md_rel, ctx)
        if resolved:
            if not is_md_rel(resolved):
                refs.add(resolved)
            continue
        actual = resolve_wiki_casefold(target, md_rel, ctx)
        if actual:
            add_case_mismatch(target, actual)
            continue
        if looks_like_code(target):
            anchors.add((md_rel, target))
            continue
        add_dead(target, "embed" if is_embed else "wikilink")

    for match in MD_LINK_RE.finditer(text):
        is_embed = bool(match.group(1))
        raw = match.group(3)
        resolved = resolve_relative(ctx["root"], md_rel, raw)
        if resolved is None:
            continue
        candidate, inside, exists = resolved
        if exists:
            full = ctx["root"] / candidate
            try:
                is_file = full.is_file()
            except OSError:
                is_file = False
            if is_file and not is_md_rel(candidate):
                refs.add(candidate)
            continue
        if (not inside) or looks_like_code(raw):
            anchors.add((md_rel, raw.strip()))
            continue
        add_dead(raw.strip(), "embed" if is_embed else "relative")

    return dead, anchors, refs, case_mismatches


def split_section_lengths(text):
    """Count body lines per `## N.` section, excluding headings and blanks."""
    counts = []
    current = None
    for line in text.splitlines():
        if SPLIT_RE.match(line):
            if current is not None:
                counts.append(current)
            current = 0
            continue
        if current is None:
            continue
        if not line.strip():
            continue
        if HEADING_LINE_RE.match(line.lstrip(" \t")):
            continue
        current += 1
    if current is not None:
        counts.append(current)
    return counts


def analyze_md(info, ctx):
    data = read_bytes(info.path, MAX_ANALYZE_BYTES)
    truncated = info.size > len(data)
    text = data.decode("utf-8-sig", errors="replace")
    bom = data.startswith(BOM)
    crlf = b"\r\n" in data

    fm_match = FRONTMATTER_RE.match(text)
    has_fm = fm_match is not None
    fields = parse_frontmatter_fields(text) if has_fm else {}
    body = FRONTMATTER_RE.sub("", text, count=1) if has_fm else text
    stripped_full, fence_unclosed = strip_fences(text)
    body_stripped, _ = strip_fences(body)
    # Comments and indented code are not links: blank them before scanning,
    # on both the full text (dead links) and the body (outbound counts).
    scan_text = strip_link_noise(stripped_full)
    body_links = strip_link_noise(body_stripped)

    h1 = None
    heading = H1_RE.search(body_stripped)
    if heading:
        h1 = heading.group(1).strip()
    title = fields.get("title") or h1 or Path(info.name).stem

    h2_count = len(H2_RE.findall(body_stripped))
    split_units = len(SPLIT_RE.findall(body_stripped))

    wiki_out = 0
    embeds = 0
    for match in WIKI_RE.finditer(body_links):
        if match.group(1):
            embeds += 1
        else:
            wiki_out += 1
    for match in MD_LINK_RE.finditer(body_links):
        if match.group(1):
            embeds += 1

    dead, anchors, refs, case_mismatches = scan_links(scan_text, info.rel, ctx)

    normalised = WS_RE.sub(" ", body).strip()
    content_hash = None
    if normalised:
        content_hash = hashlib.sha256(normalised[:2048].encode("utf-8")).hexdigest()

    section_lengths = split_section_lengths(body_stripped) if split_units else []

    obj = {
        "path": info.rel,
        "lines": len(text.splitlines()),
        "bytes": info.size,
        "mtime": _dt.datetime.fromtimestamp(info.mtime).strftime("%Y-%m-%d %H:%M:%S"),
        "has_frontmatter": has_fm,
        "title": title,
        "h1": h1,
        "h2_count": h2_count,
        "split_units": split_units,
        "wikilinks": wiki_out,
        "embeds": embeds,
        "has_table": has_table(body_stripped),
        "has_code": bool(CODE_FENCE_RE.search(body) or INDENT_CODE_RE.search(body)),
        "bom": bom,
        "crlf": crlf,
        "fence_unclosed": fence_unclosed,
        "depth": info.depth,
        "truncated": truncated,
    }
    return obj, dead, anchors, refs, case_mismatches, content_hash, section_lengths


# ---------------------------------------------------------------------------
# project / build-system detection
# ---------------------------------------------------------------------------

def resolve_doc_ref(root, base_dirs, token):
    token = token.replace("\\", "/").strip().strip("'\"").strip()
    if not token or "://" in token:
        return None
    if re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*:", token):
        return None
    if token.startswith("/"):
        token = token.lstrip("/")
        base_dirs = [""]
    candidates = []
    for base in base_dirs:
        if base:
            candidates.append(posixpath.normpath(posixpath.join(base, token)))
        else:
            candidates.append(posixpath.normpath(token))
    for candidate in candidates:
        try:
            if (root / candidate).exists():
                return candidate
        except OSError:
            pass
    return None


def extract_nav_lines(text):
    lines = text.splitlines()
    nav_indent = None
    collected = []
    for line in lines:
        if nav_indent is None:
            match = re.match(r"^(\s*)nav\s*:\s*(.*)$", line)
            if match:
                nav_indent = len(match.group(1))
                tail = match.group(2).strip()
                if tail:
                    collected.append(tail)
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= nav_indent:
            break
        collected.append(line)
    return collected


def parse_mkdocs(root, filename):
    text = read_text_file(root / filename)
    match = re.search(r"^docs_dir\s*:\s*['\"]?([^'\"#\r\n]+)", text, re.M)
    docs_dir = match.group(1).strip() if match else "docs"
    docs_dir = posixpath.normpath(docs_dir.replace("\\", "/").strip().strip("/") or "docs")
    if docs_dir in (".", ""):
        docs_dir = ""
    found = []
    targets = []
    resolved = set()

    def consider(token):
        token = token.strip().strip("'\"<>").strip()
        if not token:
            return
        if token not in targets:
            targets.append(token)
        path = resolve_doc_ref(root, [docs_dir, ""] if docs_dir else [""], token)
        if path:
            found.append((path, "%s nav" % filename))
            resolved.add(token)

    for line in extract_nav_lines(text):
        tokens = re.findall(
            r"[^\s'\"<>:,]+\.(?:md|markdown|png|jpe?g|gif|svg|pdf|html)",
            line,
            re.I,
        )
        for token in tokens:
            consider(token)
        for token in re.findall(r"([^\s'\"<>:,]+/)\s*$", line):
            consider(token)
    refs = {
        "total": len(targets),
        "existing": len(resolved),
        "missing": [token for token in targets if token not in resolved][:10],
    }
    return docs_dir, found, refs


def scan_config_md_refs(root, files):
    """Coarse fallback for doc-site configs we do not parse specifically.

    Only existing .md targets count, and only in config-like files, so a
    random string in source code cannot flip the project to docs-site.
    """
    found = []
    targets = []
    resolved = set()
    for info in files:
        if info.ext not in ("yml", "yaml", "json", "js", "mjs", "cjs", "ts"):
            continue
        if info.size > 512 * 1024:
            continue
        name = info.name.casefold()
        rel = info.rel.casefold()
        if name in ("package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock"):
            continue
        if name.startswith("mkdocs"):
            continue
        docsish = (
            name.startswith(("docusaurus.config", "sidebars"))
            or name in ("_config.yml", "_config.yaml", "book.json")
            or ".vuepress/" in rel
            or "vuepress/" in rel
            or "sidebar" in name
            or "docs" in rel
            or "site" in name
        )
        if not docsish:
            continue
        try:
            text = read_text_file(info.path)
        except OSError:
            continue
        mddir = posixpath.dirname(info.rel)
        base_dirs = [mddir, ""] if mddir else [""]
        for token in re.findall(r"[^\s'\"<>()\[\],:]+\.md\b", text):
            if "://" in token:
                continue
            token = token.lstrip("(").strip()
            if not token or token in targets:
                continue
            targets.append(token)
            path = resolve_doc_ref(root, base_dirs, token)
            if path:
                found.append((path, "%s 中的 .md 引用" % info.rel))
                resolved.add(token)
    refs = {
        "total": len(targets),
        "existing": len(resolved),
        "missing": [token for token in targets if token not in resolved][:10],
    }
    return found, refs








def collect_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            for out in collect_strings(item):
                yield out
    elif isinstance(value, dict):
        for item in value.values():
            for out in collect_strings(item):
                yield out


def parse_package_json(root):
    path = root / "package.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(read_text_file(path))
    except (ValueError, OSError):
        return []
    found = []
    for field in ("files", "main", "module", "types", "bin"):
        for value in collect_strings(data.get(field)):
            value = value.replace("\\", "/").strip()
            if not value or any(ch in value for ch in "*?["):
                continue
            if re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*:", value):
                continue
            if value.startswith("./"):
                value = value[2:]
            candidate = posixpath.normpath(value)
            try:
                if (root / candidate).exists():
                    found.append((candidate, "package.json %s" % field))
            except OSError:
                pass
    return found


def detect_project(root, md_count, files):
    system_map = {}
    doc_ref_total = 0
    doc_ref_existing = 0
    doc_ref_missing = []

    def add(name, evidence, category):
        record = system_map.setdefault(name, {"category": category, "evidence": set()})
        record["evidence"].add(evidence)

    blocked = {}
    reasons = {}

    def block(path, reason):
        if not path:
            return
        path = posixpath.normpath(path.replace("\\", "/").strip())
        if path in ("", ".", "..") or path.startswith("../") or posixpath.isabs(path):
            return
        try:
            if not (root / path).exists():
                return
        except OSError:
            return
        blocked[path] = True
        reasons.setdefault(path, reason)

    code_markers = [
        ("package.json", "npm"),
        ("package-lock.json", "npm"),
        ("pnpm-lock.yaml", "pnpm"),
        ("yarn.lock", "yarn"),
        ("pom.xml", "maven"),
        ("build.gradle", "gradle"),
        ("build.gradle.kts", "gradle"),
        ("settings.gradle", "gradle"),
        ("go.mod", "go"),
        ("Cargo.toml", "cargo"),
        ("pyproject.toml", "python"),
        ("requirements.txt", "python"),
        ("setup.py", "python"),
    ]
    for filename, name in code_markers:
        if (root / filename).is_file():
            add(name, filename, "code")

    for filename in ("mkdocs.yml", "mkdocs.yaml"):
        if (root / filename).is_file():
            add("mkdocs", filename, "docs")
            docs_dir, found, refs = parse_mkdocs(root, filename)
            doc_ref_total += refs["total"]
            doc_ref_existing += refs["existing"]
            doc_ref_missing.extend(refs["missing"])
            if docs_dir:
                block(docs_dir, "%s docs_dir" % filename)
            for path, reason in found:
                block(path, reason)

    config_found, config_refs = scan_config_md_refs(root, files)
    if config_found:
        add("config-md-refs", "配置文件中的 .md 引用", "docs")
        doc_ref_total += config_refs["total"]
        doc_ref_existing += config_refs["existing"]
        doc_ref_missing.extend(config_refs["missing"])
        for path, reason in config_found:
            block(path, reason)

    if (root / "package.json").is_file():
        for path, reason in parse_package_json(root):
            block(path, reason)

    workflows = root / ".github" / "workflows"
    if workflows.is_dir():
        try:
            has_workflow = any(workflows.glob("*.yml")) or any(workflows.glob("*.yaml"))
        except OSError:
            has_workflow = False
        if has_workflow:
            add("github-actions", ".github/workflows", "tooling")
    if (root / ".git").exists():
        add("git", ".git", "tooling")

    categories = {}
    for record in system_map.values():
        categories[record["category"]] = True
    has_code = categories.get("code", False)
    has_docs = categories.get("docs", False)
    has_obsidian = (root / ".obsidian").exists()

    if has_docs and has_code:
        kind = "mixed"
        reason = "同时检测到文档站配置与代码构建文件"
    elif has_docs:
        kind = "docs-site"
        reason = "检测到文档站配置，md 被 nav / sidebar 引用"
    elif has_code:
        if has_obsidian and md_count > 0:
            kind = "mixed"
            reason = "有代码构建文件，也有 .obsidian 配置：笔记与仓库混在一起"
        else:
            kind = "code-repo"
            reason = "有构建文件，md 是仓库附属文档"
    else:
        kind = "notes-vault"
        reason = "未发现构建文件，按纯笔记库处理"

    if kind in ("docs-site", "mixed") and has_docs:
        if doc_ref_total and not doc_ref_existing:
            reason = "检测到文档站配置，但 nav / docs_dir 引用的路径都不存在（配置与文件已脱节）"
        elif not doc_ref_total:
            reason = "检测到文档站配置，但未解析到有效的 nav / sidebar 引用"

    order = {"docs": 0, "code": 1, "tooling": 2}
    systems = []
    for name, record in sorted(
        system_map.items(), key=lambda kv: (order.get(kv[1]["category"], 9), kv[0])
    ):
        systems.append({"name": name, "evidence": sorted(record["evidence"])})

    return {
        "project_kind": kind,
        "project_kind_reason": reason,
        "build_systems": systems,
        "move_blocked": sorted(blocked),
        "move_blocked_reasons": {path: reasons[path] for path in sorted(reasons)},
        "doc_refs": {
            "total": doc_ref_total,
            "existing": doc_ref_existing,
            "missing": sorted(set(doc_ref_missing))[:10],
        },
    }


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------

def choose_deep_files(files, max_files):
    if len(files) <= max_files:
        return list(files), False
    markdown = [info for info in files if info.is_md]
    others = [info for info in files if not info.is_md]
    if len(markdown) >= max_files:
        deep = even_sample(markdown, max_files)
    else:
        deep = list(markdown) + even_sample(others, max_files - len(markdown))
    deep.sort(key=lambda info: info.rel)
    return deep, True


def group_by(items, keyfunc):
    groups = {}
    for item in items:
        groups.setdefault(keyfunc(item), []).append(item)
    return groups


def build_inventory(root, top, max_files):
    root = Path(root)
    files, dirs = enumerate_files(root)
    deep, sampled = choose_deep_files(files, max_files)
    ctx = build_indexes(files, root)

    md_total = sum(1 for info in files if info.is_md)
    md_objs = []
    split_candidates = []
    split_too_small = []
    dead_links = []
    case_mismatches = []
    anchors = set()
    refs = set()
    content_groups = {}
    bom_files = []
    crlf_files = []
    fence_unclosed = []

    for info in deep:
        if info.is_md:
            (
                obj, dead, found_anchors, found_refs, found_mismatches,
                content_hash, section_lengths,
            ) = analyze_md(info, ctx)
            md_objs.append(obj)
            if obj["split_units"] > 0:
                suggested = (
                    obj["split_units"]
                    if obj["lines"] >= 150 and obj["split_units"] >= 2
                    else 1
                )
                entry = {
                    "path": obj["path"],
                    "lines": obj["lines"],
                    "split_units": obj["split_units"],
                    "suggested_parts": suggested,
                }
                average = (
                    round(float(sum(section_lengths)) / len(section_lengths), 1)
                    if section_lengths else 0
                )
                too_short = (
                    obj["lines"] < MIN_SPLIT_FILE_LINES
                    or (
                        section_lengths
                        and all(
                            count < MIN_SECTION_BODY_LINES
                            for count in section_lengths
                        )
                    )
                )
                if too_short:
                    entry["reason"] = "过短，不建议拆（小节平均 %g 行）" % average
                    split_too_small.append(entry)
                else:
                    split_candidates.append(entry)
            dead_links.extend(dead)
            case_mismatches.extend(found_mismatches)
            anchors |= found_anchors
            refs |= found_refs
            if content_hash:
                content_groups.setdefault(content_hash, []).append(info.rel)
            if obj["bom"]:
                bom_files.append(info.rel)
            if obj["crlf"]:
                crlf_files.append(info.rel)
            if obj["fence_unclosed"]:
                fence_unclosed.append(info.rel)
        elif info.is_text:
            try:
                bom, crlf = detect_bom_crlf(info.path, TEXT_SCAN_BYTES)
            except OSError:
                bom, crlf = False, False
            if bom:
                bom_files.append(info.rel)
            if crlf:
                crlf_files.append(info.rel)

    md_objs.sort(key=lambda obj: obj["path"])
    split_candidates.sort(
        key=lambda item: (-item["split_units"], -item["lines"], item["path"])
    )
    split_too_small.sort(
        key=lambda item: (-item["split_units"], -item["lines"], item["path"])
    )

    by_ext = {}
    by_group = {group: {"count": 0, "bytes": 0} for group in GROUP_ORDER}
    total_bytes = 0
    root_md = 0
    for info in files:
        total_bytes += info.size
        by_ext[info.ext] = by_ext.get(info.ext, 0) + 1
        bucket = by_group[info.group]
        bucket["count"] += 1
        bucket["bytes"] += info.size
        if info.is_md and "/" not in info.rel:
            root_md += 1

    by_ext = {key: by_ext[key] for key in sorted(by_ext)}

    big_files = [
        {"path": info.rel, "bytes": info.size}
        for info in sorted(files, key=lambda item: (-item.size, item.rel))[:top]
    ]

    # --- duplicates -------------------------------------------------------
    stem_groups = group_by(
        [info for info in files if info.is_md],
        lambda info: Path(info.name).stem.casefold(),
    )
    by_stem = []
    for group in stem_groups.values():
        if len(group) > 1:
            paths = sorted(info.rel for info in group)
            by_stem.append({
                "stem": Path(sorted(group, key=lambda i: i.rel)[0].name).stem,
                "paths": paths,
            })
    by_stem.sort(key=lambda item: item["paths"][0])

    by_content = []
    for digest, paths in content_groups.items():
        if len(paths) > 1:
            by_content.append({"hash": digest, "paths": sorted(paths)})
    by_content.sort(key=lambda item: item["paths"][0])

    size_title_groups = group_by(
        md_objs,
        lambda obj: (obj["bytes"], str(obj["title"]).casefold()),
    )
    by_size_title = []
    for (size, title), group in size_title_groups.items():
        if len(group) > 1:
            by_size_title.append({
                "size": size,
                "title": group[0]["title"],
                "paths": sorted(obj["path"] for obj in group),
            })
    by_size_title.sort(key=lambda item: item["paths"][0])

    # --- attachments ------------------------------------------------------
    # `attachments_all` follows the literal contract: every non-md file.
    # The "attachment" view used for orphans/duplicates excludes build/config
    # and source files, which are never attachments in an Obsidian sense.
    attachments_all = [info for info in files if not info.is_md]
    attachment_ext = {}
    attachment_bytes = 0
    for info in attachments_all:
        attachment_bytes += info.size
        attachment_ext[info.ext] = attachment_ext.get(info.ext, 0) + 1

    attachment_candidates = [info for info in attachments_all if is_attachment(info)]
    real_ext = {}
    real_bytes = 0
    for info in attachment_candidates:
        real_bytes += info.size
        real_ext[info.ext] = real_ext.get(info.ext, 0) + 1

    sampled_attachments = [info for info in deep if is_attachment(info)]
    orphans = sorted(info.rel for info in sampled_attachments if info.rel not in refs)

    duplicate_attachments = []
    for _size, same_size in group_by(sampled_attachments, lambda info: info.size).items():
        if len(same_size) < 2:
            continue
        try:
            hashes = group_by(same_size, lambda info: hash_file(info.path))
        except OSError:
            continue
        for digest, same_hash in hashes.items():
            if len(same_hash) > 1:
                duplicate_attachments.append({
                    "hash": digest,
                    "paths": sorted(info.rel for info in same_hash),
                })
    duplicate_attachments.sort(key=lambda item: item["paths"][0])

    # --- dead links / anchors --------------------------------------------
    dead_links = list({
        (item["path"], item["target"], item["kind"]): item
        for item in dead_links
    }.values())
    case_mismatches = list({
        (item["path"], item["target"], item["actual"]): item
        for item in case_mismatches
    }.values())
    case_mismatches.sort(key=lambda item: (item["path"], item["target"]))
    anchor_examples = [
        "%s -> %s" % (path, target)
        for path, target in sorted(anchors)[:top]
    ]

    # --- hygiene ----------------------------------------------------------
    empty_files = sorted(info.rel for info in files if info.size == 0)
    space_names = sorted(info.rel for info in files if " " in info.name)
    deep_paths = sorted(info.rel for info in files if info.depth > DEEP_DEPTH)
    suspicious = []
    for info in files:
        lowered = info.name.casefold()
        reason = None
        if info.name.startswith("未命名"):
            reason = "未命名"
        elif "副本" in info.name:
            reason = "副本"
        elif "copy" in lowered:
            reason = "copy"
        if reason:
            suspicious.append({"path": info.rel, "reason": reason})
    suspicious.sort(key=lambda item: item["path"])

    app_json = root / ".obsidian" / "app.json"
    attachment_folder = None
    obsidian_found = (root / ".obsidian").exists()
    if app_json.is_file():
        try:
            config = json.loads(read_text_file(app_json))
            value = config.get("attachmentFolderPath")
            if isinstance(value, str) and value.strip():
                attachment_folder = value.strip()
        except (ValueError, OSError):
            pass

    hygiene = {
        "bom": sorted(bom_files),
        "crlf": sorted(crlf_files),
        "empty": empty_files,
        "space_names": space_names,
        "deep": deep_paths,
        "suspicious_names": suspicious,
        "obsidian_found": obsidian_found,
        "attachment_folder": attachment_folder,
    }

    project = detect_project(root, md_total, files)

    data = {
        "version": VERSION,
        "root": str(root),
        "sampled": sampled,
        "top": top,
        "max_files": max_files,
        "files": {
            "total": len(files),
            "md_total": md_total,
            "bytes": total_bytes,
            "by_ext": by_ext,
            "by_group": by_group,
            "root_md": root_md,
            "analyzed": len(deep),
        },
        "md": md_objs,
        "dirs": dirs,
        "split_candidates": split_candidates[:top],
        "split_too_small": split_too_small[:top],
        "big_files": big_files,
        "duplicates": {
            "by_stem": by_stem,
            "by_content": by_content,
            "by_size_title": by_size_title,
        },
        "dead_links": dead_links,
        "case_mismatch": case_mismatches,
        "code_anchors": {
            "count": len(anchors),
            "examples": anchor_examples,
        },
        "fence_unclosed": sorted(fence_unclosed),
        "attachments": {
            "total": len(attachments_all),
            "bytes": attachment_bytes,
            "by_ext": {key: attachment_ext[key] for key in sorted(attachment_ext)},
            "attachment_total": len(attachment_candidates),
            "attachment_bytes": real_bytes,
            "attachment_by_ext": {key: real_ext[key] for key in sorted(real_ext)},
            "analyzed": len(sampled_attachments),
            "orphans": orphans,
            "duplicates": duplicate_attachments,
        },
        "project_kind": project["project_kind"],
        "project_kind_reason": project["project_kind_reason"],
        "build_systems": project["build_systems"],
        "move_blocked": project["move_blocked"],
        "move_blocked_reasons": project["move_blocked_reasons"],
        "doc_refs": project["doc_refs"],
        "hygiene": hygiene,
        "recommendations": [],
    }
    data["recommendations"] = make_recommendations(data)
    return data


def make_recommendations(data):
    recs = []

    def add(text):
        if text and text not in recs:
            recs.append(text)

    kind = data["project_kind"]
    doc_refs = data.get("doc_refs") or {}
    if kind == "docs-site":
        if doc_refs.get("total") and not doc_refs.get("existing"):
            add(
                "检测到 %s，但 nav / docs_dir 引用的 %d 个路径都不存在 → 配置与文件已脱节，"
                "先核对 docs_dir 与 nav，暂不要移动文件" % (
                    _system_names(data["build_systems"]), doc_refs["total"],
                )
            )
        else:
            add(
                "项目是文档站（%s）→ 被 nav / sidebar 引用的 %d 个路径是移动禁区，"
                "先按只读处理" % (
                    _system_names(data["build_systems"]),
                    len(data["move_blocked"]),
                )
            )
    elif kind == "code-repo":
        add(
            "仓库模式：md 是附属文档 → 保留 %d 个代码锚点原样，不要改成双链，"
            "也不要按笔记库的目录骨架硬搬" % data["code_anchors"]["count"]
        )
    elif kind == "mixed":
        add(
            "仓库与笔记混在一起 → 先在映射表里划出「不动」的代码 / 文档站目录，"
            "只搬纯笔记，再决定是否分库"
        )

    splits = [
        item for item in data["split_candidates"]
        if item["lines"] >= 150 and item["split_units"] >= 2
    ]
    if splits:
        item = splits[0]
        add(
            "`%s` %d 行、%d 个 `## N.` 章节 → 建议拆 %d 篇；先把原文整份复制到 "
            "30_Sources/ 再切" % (
                item["path"], item["lines"], item["split_units"],
                item["suggested_parts"],
            )
        )

    root_md = data["files"].get("root_md", 0)
    if root_md:
        add(
            "根目录直接放着 %d 个 md → 建议先进 00_Inbox/ 再分流，别在原位改" % root_md
        )

    by_stem = data["duplicates"]["by_stem"]
    if by_stem:
        first = by_stem[0]
        add(
            "%d 组 basename 重名（如 `%s` ×%d）→ Obsidian 双链会歧义，"
            "搬家前先改名消歧" % (
                len(by_stem),
                _basename(first["paths"][0]),
                len(first["paths"]),
            )
        )

    dead = data["dead_links"]
    if dead:
        add(
            "%d 处死链（相对 / 双链 / 嵌入）→ 按「改双链 / 保留代码锚点 / "
            "改纯文本」三选一，禁止为消死链造笔记" % len(dead)
        )

    hygiene = data["hygiene"]
    dirty = len(hygiene["bom"]) + len(hygiene["crlf"])
    if dirty:
        add(
            "%d 个 BOM + %d 个 CRLF → 统一成 UTF-8 无 BOM + LF 再开始拆分" % (
                len(hygiene["bom"]), len(hygiene["crlf"]),
            )
        )
    if hygiene["empty"]:
        add("%d 个空文件 → 确认后归档，不要留在根目录" % len(hygiene["empty"]))

    if data["attachments"]["orphans"]:
        add(
            "%d 个孤儿附件 → 归入附件目录或确认后归档，别直接删" % (
                len(data["attachments"]["orphans"]),
            )
        )

    generic = [
        "改造前先 `git init` 并提交一次基线：后面每个动作都要能 `git revert`",
        "按 SKILL.md 第 2 步先产出「旧路径 → 新路径」映射表，等用户确认后再动手",
        "改造前跑一次 `python scripts/validate_vault.py <root>` 记下基线错误数，"
        "装完必须归零",
    ]
    for text in generic:
        if len(recs) >= 3:
            break
        add(text)
    return recs[:5]


def _basename(path):
    return posixpath.basename(path)


def _system_names(systems):
    names = [system["name"] for system in systems]
    return "、".join(names[:4]) if names else "无"


# ---------------------------------------------------------------------------
# human-readable output
# ---------------------------------------------------------------------------

def build_human(data, top):
    lines = []
    files = data["files"]
    sampled_note = "是（分析 %d/%d 个文件）" % (
        files["analyzed"], files["total"],
    ) if data["sampled"] else "否"
    lines.append("盘点：%s" % data["root"])
    lines.append(
        "类型：%s ｜ 文件 %d 个 / %s ｜ 抽样：%s" % (
            data["project_kind"], files["total"], human_size(files["bytes"]), sampled_note,
        )
    )

    # 1. file types
    lines.append("")
    lines.append(
        "## 1. 文件与类型（%d 个 / %s）" % (files["total"], human_size(files["bytes"]))
    )
    rows = []
    for group in GROUP_ORDER:
        bucket = files["by_group"].get(group)
        if not bucket or not bucket["count"]:
            continue
        rows.append([
            GROUP_LABELS[group], str(bucket["count"]), human_size(bucket["bytes"]),
        ])
    lines.extend(render_table(["分组", "数量", "体积"], rows, [8, 6, 10]))
    ext_items = sorted(files["by_ext"].items(), key=lambda kv: (-kv[1], kv[0]))[:6]
    lines.append(
        "扩展名：" + "、".join("%s×%d" % (ext, count) for ext, count in ext_items)
    )
    lines.append(
        "根目录直接放着 md：%d 个%s" % (
            files["root_md"], "   <- 杂乱第一现场" if files["root_md"] else "",
        )
    )
    if data["big_files"]:
        lines.append(
            "最大文件：" + "、".join(
                "%s（%s）" % (item["path"], human_size(item["bytes"]))
                for item in data["big_files"][:3]
            )
        )

    # 2. directories
    lines.append("")
    lines.append("## 2. 目录概览（%d 个目录）" % len(data["dirs"]))
    dir_rows = []
    for item in data["dirs"][:max(1, min(top, 8))]:
        dir_rows.append([
            fit_tail(item["path"], 30) if item["path"] != "." else ".",
            str(item["files"]),
            str(item["md"]),
            "是" if item["has_subdirs"] else "-",
        ])
    lines.extend(render_table(["目录", "文件", "md", "子目录"], dir_rows, [30, 6, 5, 6]))
    if len(data["dirs"]) > len(dir_rows):
        lines.append("（另有 %d 个目录，见 --json）" % (len(data["dirs"]) - len(dir_rows)))

    # 3. md summary
    lines.append("")
    lines.append(
        "## 3. md 摘要（%d 个）" % len(data["md"])
    )
    md_rows = []
    for obj in sorted(data["md"], key=lambda item: (-item["lines"], item["path"]))[:max(1, min(top, 10))]:
        md_rows.append([
            fit_tail(obj["path"], 26),
            str(obj["lines"]),
            str(obj["bytes"]),
            obj["mtime"][:10],
            "Y" if obj["has_frontmatter"] else ".",
            fit(obj["h1"] or "-", 18),
            str(obj["h2_count"]),
            str(obj["split_units"]),
            str(obj["wikilinks"]),
            str(obj["embeds"]),
            "Y" if obj["has_table"] else ".",
            "Y" if obj["has_code"] else ".",
            "Y" if obj["bom"] else ".",
            "Y" if obj["crlf"] else ".",
        ])
    if md_rows:
        lines.extend(render_table(
            ["路径", "行", "字节", "日期", "FM", "首个 H1", "#2", "切分", "链", "嵌", "表", "码", "BOM", "CRLF"],
            md_rows,
            [26, 5, 7, 10, 2, 18, 2, 4, 3, 3, 2, 2, 3, 4],
        ))
    else:
        lines.append("（未发现 md 文件）")
    if len(data["md"]) > len(md_rows):
        lines.append("（另有 %d 个 md，见 --json）" % (len(data["md"]) - len(md_rows)))

    # 4. split candidates
    lines.append("")
    lines.append("## 4. 切分候选（%d 个）" % len(data["split_candidates"]))
    split_rows = []
    for item in data["split_candidates"][:max(1, min(top, 8))]:
        split_rows.append([
            fit_tail(item["path"], 36),
            str(item["lines"]),
            str(item["split_units"]),
            str(item["suggested_parts"]),
        ])
    if split_rows:
        lines.extend(render_table(["路径", "行", "切分", "建议"], split_rows, [36, 6, 4, 4]))
    else:
        lines.append("（没有发现 `## N.` 形式的切分单位）")
    too_small = data.get("split_too_small") or []
    if too_small:
        lines.append("过短，不建议拆（不计入主榜）：")
        for item in too_small[:max(1, min(top, 5))]:
            lines.append(
                "  %s（%d 行）%s" % (
                    fit_tail(item["path"], 40), item["lines"],
                    item.get("reason", ""),
                )
            )
        if len(too_small) > 5:
            lines.append("  ... 另有 %d 个" % (len(too_small) - 5))

    # 5. duplicates
    duplicates = data["duplicates"]
    lines.append("")
    lines.append(
        "## 5. 疑似重复（重名 %d 组 / 同前缀 %d 组 / 同字节同标题 %d 组）" % (
            len(duplicates["by_stem"]), len(duplicates["by_content"]),
            len(duplicates["by_size_title"]),
        )
    )
    lines.append("basename 重名（Obsidian 双链会歧义）：")
    if duplicates["by_stem"]:
        for item in duplicates["by_stem"][:max(1, min(top, 3))]:
            lines.append(
                "  %s ×%d：%s" % (
                    item["stem"], len(item["paths"]), "  =  ".join(item["paths"]),
                )
            )
    else:
        lines.append("  无")
    lines.append("正文前 2KB 相同：")
    if duplicates["by_content"]:
        for item in duplicates["by_content"][:max(1, min(top, 3))]:
            lines.append("  " + "  =  ".join(item["paths"]))
    else:
        lines.append("  无")
    lines.append("同字节数 + 同标题：")
    if duplicates["by_size_title"]:
        for item in duplicates["by_size_title"][:max(1, min(top, 3))]:
            lines.append(
                "  %s（%d 字节）：%s" % (
                    item["title"], item["size"], "  =  ".join(item["paths"]),
                )
            )
    else:
        lines.append("  无")

    # 6. dead links
    dead = data["dead_links"]
    kind_counts = {"relative": 0, "wikilink": 0, "embed": 0}
    for item in dead:
        kind_counts[item["kind"]] = kind_counts.get(item["kind"], 0) + 1
    lines.append("")
    lines.append(
        "## 6. 死链预扫（死链 %d 处 / 代码锚点 %d 处）" % (
            len(dead), data["code_anchors"]["count"],
        )
    )
    lines.append(
        "相对 %d ｜ 双链 %d ｜ 嵌入 %d ｜ 代码锚点属正常，勿改勿删" % (
            kind_counts["relative"], kind_counts["wikilink"], kind_counts["embed"],
        )
    )
    if dead:
        for item in dead[:max(1, min(top, 6))]:
            lines.append(
                "  [%s] %s -> %s" % (
                    item["kind"], fit_tail(item["path"], 34),
                    fit(item["target"], 40),
                )
            )
        if len(dead) > 6:
            lines.append("  ... 还有 %d 处" % (len(dead) - 6))
    else:
        lines.append("  未发现死链")
    if data["code_anchors"]["examples"]:
        lines.append("代码锚点示例：" + "；".join(data["code_anchors"]["examples"][:3]))
    if data.get("fence_unclosed"):
        lines.append(
            "围栏未闭合：%s（其后链接未检查）" % "、".join(data["fence_unclosed"][:3])
        )
        if len(data["fence_unclosed"]) > 3:
            lines.append("  ... 另有 %d 个" % (len(data["fence_unclosed"]) - 3))

    # 7. case mismatch
    mismatches = data.get("case_mismatch") or []
    lines.append("")
    lines.append(
        "## 7. 大小写不一致（%d 处；校验器会判 broken-wikilink，"
        "Obsidian 在 Windows 上能打开——跨平台库必须改成一致）" % len(mismatches)
    )
    if mismatches:
        for item in mismatches[:max(1, min(top, 6))]:
            lines.append(
                "  %s -> %s（实际 %s）" % (
                    fit_tail(item["path"], 34), fit(item["target"], 30),
                    fit(item["actual"], 34),
                )
            )
        if len(mismatches) > 6:
            lines.append("  ... 还有 %d 处" % (len(mismatches) - 6))
    else:
        lines.append("  未发现大小写不一致")

    # 8. attachments
    attachments = data["attachments"]
    lines.append("")
    lines.append(
        "## 8. 附件（%d 个 / %s）" % (
            attachments["attachment_total"], human_size(attachments["attachment_bytes"]),
        )
    )
    att_items = sorted(attachments["attachment_by_ext"].items(), key=lambda kv: (-kv[1], kv[0]))[:6]
    if att_items:
        lines.extend(render_table(
            ["扩展名", "数量"],
            [[ext, str(count)] for ext, count in att_items],
            [10, 6],
        ))
        if len(attachments["attachment_by_ext"]) > len(att_items):
            lines.append("（另有 %d 种扩展名）" % (
                len(attachments["attachment_by_ext"]) - len(att_items)
            ))
    else:
        lines.append("  无附件")
    other_non_md = attachments["total"] - attachments["attachment_total"]
    if other_non_md > 0:
        lines.append("另有 %d 个配置 / 源码等非 md 文件，已计入文件总量，不计入附件" % other_non_md)
    lines.append(
        "孤儿附件：%d 个%s" % (
            len(attachments["orphans"]),
            ("：" + "、".join(attachments["orphans"][:4])) if attachments["orphans"] else "",
        )
    )
    if len(attachments["orphans"]) > 4:
        lines.append("  ... 另有 %d 个" % (len(attachments["orphans"]) - 4))
    lines.append("重复附件：%d 组" % len(attachments["duplicates"]))
    for item in attachments["duplicates"][:max(1, min(top, 3))]:
        lines.append("  " + "  =  ".join(item["paths"]))

    # 9. project
    lines.append("")
    lines.append(
        "## 9. 工程类型（%s，%d 个标志）" % (
            data["project_kind"], len(data["build_systems"]),
        )
    )
    lines.append("判断依据：" + data["project_kind_reason"])
    if data["build_systems"]:
        lines.append(
            "构建 / 工程标志：" + "；".join(
                "%s（%s）" % (system["name"], "、".join(system["evidence"]))
                for system in data["build_systems"][:6]
            )
        )
    else:
        lines.append("构建 / 工程标志：无")
    doc_refs = data.get("doc_refs") or {}
    if doc_refs.get("total"):
        suffix = ""
        if not doc_refs.get("existing"):
            suffix = "（配置与文件已脱节，先核对 docs_dir 与 nav）"
        lines.append(
            "导航引用 %d 个路径，其中 %d 个存在%s" % (
                doc_refs["total"], doc_refs.get("existing", 0), suffix,
            )
        )
        if doc_refs.get("missing"):
            lines.append("  未找到：" + "、".join(doc_refs["missing"][:4]))
    if data["move_blocked"]:
        lines.append(
            "移动禁区（%d）：%s" % (
                len(data["move_blocked"]), "、".join(data["move_blocked"][:8]),
            )
        )
        if len(data["move_blocked"]) > 8:
            lines.append("  ... 另有 %d 个" % (len(data["move_blocked"]) - 8))
    elif doc_refs.get("total"):
        lines.append("移动禁区：无（配置引用的路径都不存在，先不要据此移动文件）")
    else:
        lines.append("移动禁区：无（未解析到 nav / docs_dir 引用）")

    # 10. hygiene
    hygiene = data["hygiene"]
    lines.append("")
    lines.append(
        "## 10. 卫生（BOM %d / CRLF %d / 空 %d / 空格名 %d / 深路径 %d / 可疑命名 %d）" % (
            len(hygiene["bom"]), len(hygiene["crlf"]), len(hygiene["empty"]),
            len(hygiene["space_names"]), len(hygiene["deep"]),
            len(hygiene["suspicious_names"]),
        )
    )
    for label, items in (
        ("BOM", hygiene["bom"]),
        ("CRLF", hygiene["crlf"]),
        ("空文件", hygiene["empty"]),
        ("空格名", hygiene["space_names"]),
        ("深路径", hygiene["deep"]),
    ):
        if items:
            lines.append("  %s：%s%s" % (
                label, "、".join(items[:3]), " 等" if len(items) > 3 else "",
            ))
    if hygiene["suspicious_names"]:
        lines.append("  可疑命名：" + "、".join(
            "%s（%s）" % (item["path"], item["reason"])
            for item in hygiene["suspicious_names"][:3]
        ))
    if hygiene["attachment_folder"]:
        lines.append(
            "附件目录设置：%s（.obsidian/app.json）" % hygiene["attachment_folder"]
        )
    elif hygiene["obsidian_found"]:
        lines.append("附件目录设置：未设置（.obsidian/app.json）")
    else:
        lines.append("附件目录设置：未发现 .obsidian/app.json")

    # 11. recommendations
    lines.append("")
    lines.append("## 11. 建议的下一步（%d 条）" % len(data["recommendations"]))
    for index, text in enumerate(data["recommendations"], 1):
        lines.append("%d. %s" % (index, text))

    return lines


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(
        prog="inventory.py",
        description="盘点 Markdown / Obsidian 库（只读，零第三方依赖）",
    )
    parser.add_argument("root", help="库根目录")
    parser.add_argument("--json", action="store_true", help="只输出一个 JSON 对象")
    parser.add_argument(
        "--top", type=int, default=DEFAULT_TOP,
        help="大文件 / 最长列表取前 N（默认 %d）" % DEFAULT_TOP,
    )
    parser.add_argument(
        "--max-files", type=int, default=DEFAULT_MAX_FILES,
        help="超过 N 个文件时抽样分析并标记 sampled（默认 %d）" % DEFAULT_MAX_FILES,
    )
    args = parser.parse_args(argv)

    if args.top < 1 or args.max_files < 1:
        parser.error("--top 与 --max-files 必须是正整数")

    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print("错误：路径不存在或不是目录：%s" % args.root, file=sys.stderr)
        return 2

    data = build_inventory(root, args.top, args.max_files)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print("\n".join(build_human(data, args.top)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
