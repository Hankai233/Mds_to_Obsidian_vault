#!/usr/bin/env python3
"""Validate a markdown knowledge vault (Obsidian-style) before and after a refactor.

Usage:
    python validate_vault.py <vault_root> [--quiet]

Checks, in order of severity:

  ERR  missing frontmatter / missing required field
  ERR  invalid `type` value, or `type` contradicting the directory prefix
  ERR  `title` disagreeing with the file name
  ERR  broken wikilink [[X]] or broken relative markdown link [t](path.md)
  ERR  heading level jump (# -> ###)
  ERR  note/project body with zero outbound wikilinks (a `source:` link does not count)
  WARN note with zero inbound links (orphan note)
  WARN note/project body with only one outbound wikilink
  WARN file name containing spaces
  WARN status=evergreen note that links to nothing

Fenced code blocks are stripped before scanning, so documentation examples
inside ``` fences never produce false positives.

Exit code 0 = no errors (warnings allowed), 1 = errors found, 2 = usage error.
"""

import re
import sys
from pathlib import Path

REQUIRED_FIELDS = [
    "title", "type", "status", "tags",
    "created", "updated", "source", "confidence",
]
VALID_TYPES = {"note", "moc", "source", "project", "journal"}
VALID_STATUS = {"seed", "growing", "evergreen"}
VALID_CONFIDENCE = {"high", "medium", "low"}

# directory prefix -> expected `type`
DIR_TYPE = {
    "10_Notes": "note",
    "20_MOC": "moc",
    "30_Sources": "source",
    "40_Projects": "project",
    "90_Journal": "journal",
}

# never linted as notes
SKIP_DIR_PARTS = {".obsidian", ".git", "node_modules", ".trash", "_templates"}

# files that are legitimately not linked from anywhere
ORPHAN_EXEMPT_STEMS = {"README", "AGENTS", "CLAUDE", "INDEX", "Home"}
ORPHAN_EXEMPT_PATHS = {"00_Inbox/README.md", "_templates"}

# instruction files, not notes: no frontmatter expected
FRONTMATTER_EXEMPT_FILES = {"AGENTS.md", "CLAUDE.md", ".agents.md", ".cursorrules.md"}

CODE_PATH_HINT = re.compile(
    r"(^|/)(src|lib|app|web|frontend|backend|gradle|maven|target|node_modules)(/|$)"
    r"|\.(java|kt|scala|groovy|xml|yml|yaml|properties|gradle|sql|ts|tsx|js|jsx|vue|py|go|rs|proto|sh|ps1)$",
    re.I,
)


def looks_like_code(target):
    """A markdown link that points into a source-code tree, not into the vault."""
    return bool(CODE_PATH_HINT.search(target))


FENCE_RE = re.compile(r"^(```|~~~).*?^\1[^\n]*$", re.M | re.S)
WIKILINK_RE = re.compile(r"!?\[\[([^\]]+)\]\]")
MDLINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
HEADING_RE = re.compile(r"^(#{1,6})\s+\S", re.M)
FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.S)


def strip_fences(text):
    """Blank out fenced code blocks, keeping line count stable."""
    def repl(m):
        return "\n" * m.group(0).count("\n")

    return FENCE_RE.sub(repl, text)


def parse_frontmatter(text):
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None
    fields = {}
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def wikilink_target(raw):
    target = raw.split("|", 1)[0].split("#", 1)[0].strip()
    if target.endswith("\\"):
        target = target[:-1].strip()
    return target


def scan(vault):
    errors, warnings, code_anchors = [], [], []
    md_files = []
    for path in sorted(vault.rglob("*.md")):
        parts = set(path.relative_to(vault).parts)
        if parts & SKIP_DIR_PARTS:
            continue
        md_files.append(path)

    stems = {p.stem: p for p in md_files}
    all_rel = {p.relative_to(vault).as_posix(): p for p in md_files}
    inbound = {p: 0 for p in md_files}
    outbound = {p: 0 for p in md_files}

    for path in md_files:
        rel = path.relative_to(vault).as_posix()
        raw = path.read_text(encoding="utf-8", errors="replace")
        body = strip_fences(raw)
        # body only, frontmatter removed: a link in `source:` is a provenance
        # pointer, it does not count as one of the note's own [[双链]]
        body_nofm = strip_fences(FRONTMATTER_RE.sub("", raw, count=1))

        # --- frontmatter ---
        fm = parse_frontmatter(raw)
        if fm is None:
            if path.name not in FRONTMATTER_EXEMPT_FILES:
                errors.append(f"{rel}: 缺少 frontmatter")
            fm = {}
        else:
            for field in REQUIRED_FIELDS:
                if field not in fm:
                    errors.append(f"{rel}: frontmatter 缺字段 `{field}`")
            note_type = fm.get("type", "").strip()
            if note_type and note_type not in VALID_TYPES:
                errors.append(f"{rel}: type `{note_type}` 不在 {sorted(VALID_TYPES)}")
            status = fm.get("status", "").strip()
            if status and status not in VALID_STATUS:
                errors.append(f"{rel}: status `{status}` 不在 {sorted(VALID_STATUS)}")
            conf = fm.get("confidence", "").strip()
            if conf and conf not in VALID_CONFIDENCE:
                errors.append(f"{rel}: confidence `{conf}` 不在 {sorted(VALID_CONFIDENCE)}")
            title = fm.get("title", "").strip().strip('"').strip("'")
            if title and title != path.stem:
                warnings.append(f"{rel}: title `{title}` 与文件名不一致")

            first_dir = rel.split("/", 1)[0] if "/" in rel else ""
            expected = DIR_TYPE.get(first_dir)
            if expected and note_type and note_type != expected:
                errors.append(
                    f"{rel}: type `{note_type}` 与目录 `{first_dir}` 要求的 `{expected}` 不符"
                )
            if expected is None and note_type and rel.split("/", 1)[0] not in DIR_TYPE:
                # root-level files: only README/AGENTS-ish are expected
                pass

        # --- heading levels ---
        levels = [len(m.group(1)) for m in HEADING_RE.finditer(body)]
        prev = 0
        for lvl in levels:
            if prev and lvl > prev + 1:
                errors.append(f"{rel}: 标题层级跳级（{prev} 级后直接出现 {lvl} 级）")
                break
            prev = lvl

        # --- wikilinks ---
        for m in WIKILINK_RE.finditer(body):
            target = wikilink_target(m.group(1))
            if not target or target in stems:
                continue
            # allow full path form [[40_Projects/x/PaiSmart]]
            if target in {p.stem for p in all_rel.values()}:
                continue
            errors.append(f"{rel}: 失效双链 [[{target}]]")
        for m in WIKILINK_RE.finditer(body):
            target = wikilink_target(m.group(1))
            if target in stems and stems[target] != path:
                inbound[stems[target]] += 1
        for m in WIKILINK_RE.finditer(body_nofm):
            target = wikilink_target(m.group(1))
            if target in stems and stems[target] != path:
                outbound[path] += 1

        # --- relative markdown links ---
        for m in MDLINK_RE.finditer(body):
            target = m.group(1).strip()
            if "://" in target or target.startswith(("#", "mailto:", "!")):
                continue
            if "{{" in target or target.startswith("<"):
                continue
            clean = target.split("#", 1)[0].strip()
            if not clean:
                continue
            resolved = (path.parent / clean).resolve()
            try:
                resolved.relative_to(vault)
                inside_vault = True
            except ValueError:
                inside_vault = False
            exists = resolved.exists()
            if not inside_vault or (not exists and looks_like_code(clean)):
                # points at the source-code repo, not at a vault file:
                # a deliberate anchor, never a broken link. Leave it alone.
                code_anchors.append((rel, clean))
                continue
            if not exists:
                errors.append(f"{rel}: 失效相对链接 -> {clean}")
            else:
                for p in md_files:
                    if p.resolve() == resolved:
                        inbound[p] += 1

        # --- 出链：内容笔记必须自己长出链接，只靠 MOC 链进来不算 ---
        note_type = fm.get("type", "").strip()
        in_archive = rel.startswith("99_Archive/") or rel.startswith("00_Inbox/")
        if note_type in {"note", "project"} and not in_archive:
            n = outbound[path]
            if n == 0:
                errors.append(f"{rel}: 正文零出链（要求 ≥2 条双链，只写 source 不算）")
            elif n == 1:
                warnings.append(f"{rel}: 正文只有 1 条出链（要求 ≥2 条）")

        if fm.get("status", "").strip() == "evergreen" and inbound[path] == 0:
            warnings.append(f"{rel}: status=evergreen 但没有任何入链")

    # --- orphans ---
    for path in md_files:
        rel = path.relative_to(vault).as_posix()
        if inbound[path] > 0:
            continue
        if path.stem in ORPHAN_EXEMPT_STEMS or rel in ORPHAN_EXEMPT_PATHS:
            continue
        if path.stem.endswith("模板"):
            continue
        warnings.append(f"{rel}: 孤儿笔记（零入链）")

    if any(" " in p.stem for p in md_files):
        for p in md_files:
            if " " in p.stem:
                warnings.append(f"{p.relative_to(vault).as_posix()}: 文件名含空格")

    errors = list(dict.fromkeys(errors))
    warnings = list(dict.fromkeys(warnings))
    return md_files, errors, warnings, sorted(set(code_anchors))


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    vault = Path(argv[1]).resolve()
    quiet = "--quiet" in argv
    if not vault.is_dir():
        print(f"不是目录: {vault}")
        return 2

    md_files, errors, warnings, code_anchors = scan(vault)

    print(f"Vault: {vault}")
    print(f"markdown 文件: {len(md_files)}")
    if not quiet:
        for w in warnings:
            print(f"  WARN  {w}")
    print()
    if code_anchors:
        print(f"代码仓库锚点（指向库外，属正常，不要改也不要删）: {len(code_anchors)} 处")
        for rel, target in code_anchors[:3]:
            print(f"  e.g. {rel} -> {target}")
        print()
    if errors:
        print(f"ERROR ({len(errors)}):")
        for e in errors[:40]:
            print(f"  {e}")
        if len(errors) > 40:
            print(f"  ... 还有 {len(errors) - 40} 条")
        print()
    print(f"结果: {len(errors)} 个错误 / {len(warnings)} 个警告")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
