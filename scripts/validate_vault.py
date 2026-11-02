#!/usr/bin/env python3
"""Validate a markdown knowledge vault (Obsidian-style) before and after a refactor.

用法：
    python validate_vault.py <vault_root> [--quiet] [--json] [--max-errors N]

退出码：0 = 无错误（警告允许），1 = 有错误，2 = 用法错误。

检查项（ERROR，rule id 固定，不可改名）：

    frontmatter-missing      缺 frontmatter（根目录 README.md 与 agent 指令文件豁免）
    field-missing            frontmatter 缺必需字段（message 写出字段名）
    field-unknown            出现 8 个固定字段之外的键
    type-invalid             type 不在枚举内
    status-invalid           status 不在枚举内
    confidence-invalid       confidence 不在枚举内
    date-invalid             created / updated 不是 YYYY-MM-DD
    type-dir-mismatch        type 与目录前缀矛盾
    broken-wikilink          非嵌入的 [[X]] 解析不到
    broken-embed             ![[x.png]] 这类附件嵌入在全库找不到
    broken-relative-link     相对链接指向库内不存在的文件
    heading-jump             标题层级跳级（# 下直接出现 ###）
    no-outbound              type: note/project 正文零出链（source: 里的双链不算）
    dup-stem                 全库重名 .md（basename 相同，[[X]] 会歧义）
    source-broken            source 为 [[X]] 时 X 不存在或不是 type: source

检查项（WARN，rule id 固定）：

    orphan                   孤儿笔记（零入链）
    one-outbound             正文只有 1 条出链
    title-mismatch           title 与文件名 / 首个 H1 不一致
    filename-space           文件名含空格
    evergreen-no-inbound     status=evergreen 但无任何入链
    root-note                vault 根目录直接放笔记（README/AGENTS/CLAUDE/INDEX/Home 例外）
    multi-h1                 正文出现多于一个一级标题
    no-h1                    正文一个一级标题都没有
    fence-unclosed           代码围栏开了没闭合（未闭合部分视为延伸到文件末尾）
    not-indexed-by-moc       type: note/project 的入链里没有来自 type: moc 的笔记

约定：
  * 围栏代码块（``` 或 ~~~，允许行首缩进）先被剥离再扫链接，未闭合的围栏延伸到文件末尾。
  * ![[x.png]] 且扩展名属于附件集时按「附件嵌入」处理：找到即静默通过，不计入
    inbound/outbound；找不到报 broken-embed。![[某笔记]] 仍按普通双链处理。
  * [[notes/a/Guide]] 这种全路径双链先按「去掉 .md 的库内相对路径」匹配，
    再退回 basename 匹配。
  * 指向代码仓库的锚点（../src/main/java/...、Foo.java:12-34）属正常情况，
    单独汇总成 code_anchors，不报错。
  * 00_Inbox/ 与 99_Archive/ 豁免出链检查。
"""

import json
import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import unquote as url_unquote

# frontmatter 只允许这 8 个字段
REQUIRED_FIELDS = [
    "title", "type", "status", "tags",
    "created", "updated", "source", "confidence",
]
VALID_TYPES = {"note", "moc", "source", "project", "journal"}
VALID_STATUS = {"seed", "growing", "evergreen"}
VALID_CONFIDENCE = {"high", "medium", "low"}

# 目录前缀 -> 期望的 type
DIR_TYPE = {
    "10_Notes": "note",
    "20_MOC": "moc",
    "30_Sources": "source",
    "40_Projects": "project",
    "90_Journal": "journal",
}

# 这些目录整棵跳过
SKIP_DIR_PARTS = {".obsidian", ".git", "node_modules", ".trash", "_templates"}

# 附件扩展名：![[x.png]] 按「文件存在性」检查，不算双链
ATTACHMENT_EXTS = {
    "png", "jpg", "jpeg", "gif", "svg", "webp", "bmp", "avif",
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
    "epub", "mp3", "mp4", "webm", "mov", "wav", "m4a",
    "csv", "zip", "canvas", "excalidraw",
}

# 合法但「不被谁链入」也算正常的文件
ORPHAN_EXEMPT_STEMS = {"README", "AGENTS", "CLAUDE", "INDEX", "Home"}
ORPHAN_EXEMPT_PATHS = {"00_Inbox/README.md"}
# 收件箱与归档豁免出链数量、MOC 索引两类检查
EXEMPT_LINK_CHECK_PREFIXES = ("00_Inbox/", "99_Archive/")

# 指令文件本来就没有 frontmatter（按文件名豁免，任意层级）
FRONTMATTER_EXEMPT_FILES = {"AGENTS.md", "CLAUDE.md", ".agents.md", ".cursorrules.md"}
# 根目录的 README 是给人看的说明，不要求 frontmatter
FRONTMATTER_EXEMPT_ROOT_FILES = {"README.md"}

# 根目录允许直接放的文件（其余 .md 报 root-note）
ROOT_NOTE_EXEMPT_STEMS = {"README", "AGENTS", "CLAUDE", "INDEX", "Home"}

CODE_PATH_HINT = re.compile(
    r"(^|/)(src|lib|app|web|frontend|backend|gradle|maven|target|node_modules)(/|$)"
    r"|\.(java|kt|scala|groovy|xml|yml|yaml|properties|gradle|sql|ts|tsx|js|jsx|vue|py|go|rs|proto|sh|ps1)$",
    re.I,
)

FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
# 第一个捕获组是 `!`，用来区分普通双链与嵌入
WIKILINK_RE = re.compile(r"(!?)\[\[([^\]\n]+)\]\]")
# 允许 [t](<../assets/带 空格.pdf>) 这种尖括号包裹的路径
MDLINK_RE = re.compile(r"\[[^\]]*\]\(\s*(<[^>]*>|[^)\s]+)\s*\)")
HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+\S", re.M)
H1_RE = re.compile(r"^ {0,3}#[ \t]+(.+?)[ \t]*$", re.M)
FENCE_OPEN_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})(.*)$")
FENCE_CLOSE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})[ \t]*$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def looks_like_code(target):
    """判断一个 markdown 链接是否指向源码树而不是本库文件。"""
    return bool(CODE_PATH_HINT.search(target))


def strip_fences(text):
    """把围栏代码块替换成空行（保持行数），并返回是否有未闭合围栏。

    支持 ``` 与 ~~~、允许行首缩进；未闭合的围栏视为延伸到文件末尾。
    """
    out = []
    fence_char = None
    fence_len = 0
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        keyword = "\n" if line.endswith(("\n", "\r")) else ""
        if fence_char is None:
            m = FENCE_OPEN_RE.match(stripped)
            # 反引号围栏的信息串里不允许再出现反引号（否则是行内代码）
            if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                fence_char = m.group(1)[0]
                fence_len = len(m.group(1))
                out.append(keyword)
                continue
            out.append(line)
            continue
        m = FENCE_CLOSE_RE.match(stripped)
        if m and m.group(1)[0] == fence_char and len(m.group(1)) >= fence_len:
            fence_char = None
            fence_len = 0
        out.append(keyword)
    return "".join(out), fence_char is not None


def split_frontmatter(text):
    """返回 (frontmatter 文本 or None, 去掉 frontmatter 后的正文)。"""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None, text
    return m.group(1), text[m.end():]


def unquote(value):
    """去掉 YAML 值外面的引号。"""
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v.strip()


def parse_fields(block):
    """解析 frontmatter 的简单 key: value。

    支持 `tags: [a, b]`（值原样保留）和多行列表：值为空的字段后面的 `- item`
    行属于列表内容，不会变成字段。复杂 YAML 不做处理。
    """
    fields = {}
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- "):
            # 多行列表项，不是字段
            continue
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        if not key or re.search(r"\s", key):
            continue
        fields[key] = value.strip()
    return fields


def valid_date(value):
    """必须是 YYYY-MM-DD，且是真实存在的日期。"""
    if not DATE_RE.match(value):
        return False
    try:
        date(int(value[:4]), int(value[5:7]), int(value[8:10]))
    except ValueError:
        return False
    return True


def wikilink_target(raw):
    """从 [[...]] 内部取出目标：去掉别名、锚点与转义反斜杠。"""
    target = raw.split("|", 1)[0].split("#", 1)[0].strip()
    if target.endswith("\\"):
        target = target[:-1].strip()
    return target


def iter_links(text):
    """产出 (是否嵌入, 目标)。"""
    for m in WIKILINK_RE.finditer(text):
        target = wikilink_target(m.group(2))
        if target:
            yield m.group(1) == "!", target


def is_attachment(target):
    name = target.replace("\\", "/").rsplit("/", 1)[-1]
    if "." not in name:
        return False
    return name.rsplit(".", 1)[-1].lower() in ATTACHMENT_EXTS


def resolve_wikilink(target, stem_index, rel_noext, rel_path):
    """把 [[X]] 解析成候选 .md 路径列表（空列表 = 失效）。"""
    norm = target.replace("\\", "/").strip().lstrip("/")
    if not norm:
        return []
    noext = norm[:-3] if norm.lower().endswith(".md") else norm
    if norm.lower().endswith(".md") and norm in rel_path:
        return [rel_path[norm]]
    if "/" in noext:
        hit = rel_noext.get(noext)
        if hit is not None:
            return [hit]
        if norm in rel_path:
            return [rel_path[norm]]
        return stem_index.get(noext.rsplit("/", 1)[-1], [])
    return stem_index.get(noext, [])


def resolve_attachment(target, file_names, file_rels):
    """附件嵌入按全库文件名查找（带扩展名）。"""
    norm = target.replace("\\", "/").strip().lstrip("/")
    if not norm:
        return False
    if "/" in norm:
        if norm in file_rels:
            return True
        norm = norm.rsplit("/", 1)[-1]
    return norm in file_names


def collect_files(vault):
    """一次遍历，同时收集 md 与全部文件（附件索引需要）。"""
    md_files, all_files = [], []
    for path in sorted(vault.rglob("*")):
        if not path.is_file():
            continue
        rel_parts = set(path.relative_to(vault).parts)
        if rel_parts & SKIP_DIR_PARTS:
            continue
        all_files.append(path)
        if path.suffix.lower() == ".md":
            md_files.append(path)
    return md_files, all_files


def frontmatter_exempt(rel, name):
    if name in FRONTMATTER_EXEMPT_FILES:
        return True
    return "/" not in rel and name in FRONTMATTER_EXEMPT_ROOT_FILES


class Findings:
    """错误 / 警告收集器，按 (path, rule, message) 去重且保留首次出现顺序。"""

    def __init__(self):
        self.errors = []
        self.warnings = []
        self._seen = set()

    def add(self, bucket, path, rule, message):
        key = (bucket, path, rule, message)
        if key in self._seen:
            return
        self._seen.add(key)
        item = {"path": path, "rule": rule, "message": message}
        (self.errors if bucket == "error" else self.warnings).append(item)


def scan(vault):
    findings = Findings()
    md_files, all_files = collect_files(vault)

    # --- 索引：避免对每个链接都全库扫描 ---
    stem_index = {}
    rel_noext = {}
    rel_path = {}
    for p in md_files:
        stem_index.setdefault(p.stem, []).append(p)
        rel = p.relative_to(vault).as_posix()
        rel_path[rel] = p
        rel_noext[rel[:-3]] = p
    file_names = {p.name for p in all_files}
    file_rels = {p.relative_to(vault).as_posix() for p in all_files}

    # --- 第一遍：读文件 + 解析 frontmatter / 正文 ---
    records = []
    for path in md_files:
        rel = path.relative_to(vault).as_posix()
        raw = path.read_text(encoding="utf-8-sig", errors="replace")
        fm_block, nofm = split_frontmatter(raw)
        fm = parse_fields(fm_block) if fm_block is not None else {}
        body, fence_unclosed = strip_fences(raw)      # 含 frontmatter：算入链
        body_nofm, _ = strip_fences(nofm)             # 不含 frontmatter：算出链
        records.append({
            "path": path,
            "rel": rel,
            "fm": fm,
            "fm_block": fm_block,
            "body": body,
            "body_nofm": body_nofm,
            "fence_unclosed": fence_unclosed,
        })

    fm_by_path = {r["path"]: r["fm"] for r in records}
    type_by_path = {
        r["path"]: unquote(fm_by_path[r["path"]].get("type", "")) for r in records
    }

    inbound_sources = {}   # 目标 -> 来源集合
    outbound = {}          # 来源 -> 正文出链条数
    anchors = []           # (路径, 目标)

    def note_inbound(dest, source):
        if dest == source:
            return
        inbound_sources.setdefault(dest, set()).add(source)

    # --- 第二遍：逐文件检查 ---
    for rec in records:
        path = rec["path"]
        rel = rec["rel"]
        fm = rec["fm"]
        fm_block = rec["fm_block"]
        body = rec["body"]
        body_nofm = rec["body_nofm"]
        # 说明文档（README/AGENTS 等）不是笔记：豁免 frontmatter 与标题类检查
        fm_exempt = frontmatter_exempt(rel, path.name)

        # ---- frontmatter ----
        if fm_block is None:
            if not fm_exempt:
                findings.add("error", rel, "frontmatter-missing", "缺少 frontmatter")
        else:
            for field in REQUIRED_FIELDS:
                if field not in fm:
                    findings.add(
                        "error", rel, "field-missing",
                        f"frontmatter 缺字段 `{field}`",
                    )
            unknown = sorted(set(fm) - set(REQUIRED_FIELDS))
            if unknown:
                findings.add(
                    "error", rel, "field-unknown",
                    f"未知字段 {', '.join('`%s`' % k for k in unknown)}"
                    f"（只允许 8 个字段: {' '.join(REQUIRED_FIELDS)}）",
                )
            note_type = unquote(fm.get("type", ""))
            if note_type and note_type not in VALID_TYPES:
                findings.add(
                    "error", rel, "type-invalid",
                    f"type `{note_type}` 不在 {sorted(VALID_TYPES)}",
                )
            status = unquote(fm.get("status", ""))
            if status and status not in VALID_STATUS:
                findings.add(
                    "error", rel, "status-invalid",
                    f"status `{status}` 不在 {sorted(VALID_STATUS)}",
                )
            confidence = unquote(fm.get("confidence", ""))
            if confidence and confidence not in VALID_CONFIDENCE:
                findings.add(
                    "error", rel, "confidence-invalid",
                    f"confidence `{confidence}` 不在 {sorted(VALID_CONFIDENCE)}",
                )
            for field in ("created", "updated"):
                if field in fm:
                    value = unquote(fm[field])
                    if not valid_date(value):
                        findings.add(
                            "error", rel, "date-invalid",
                            f"{field} `{value}` 不是合法的 YYYY-MM-DD",
                        )
            first_dir = rel.split("/", 1)[0] if "/" in rel else ""
            expected = DIR_TYPE.get(first_dir)
            if expected and note_type and note_type != expected:
                findings.add(
                    "error", rel, "type-dir-mismatch",
                    f"type `{note_type}` 与目录 `{first_dir}` 要求的 `{expected}` 不符",
                )

        note_type = unquote(fm.get("type", ""))
        title = unquote(fm.get("title", ""))

        # ---- 标题：H1 数量 / 层级跳级 ----
        h1_list = [m.group(1).strip() for m in H1_RE.finditer(body_nofm)]
        if not fm_exempt:
            if not h1_list:
                findings.add("warn", rel, "no-h1", "正文没有一级标题（# 标题）")
            elif len(h1_list) > 1:
                findings.add(
                    "warn", rel, "multi-h1",
                    f"正文有 {len(h1_list)} 个一级标题（只允许 1 个）: "
                    + " / ".join("# " + t for t in h1_list),
                )
            if title:
                if title != path.stem:
                    findings.add(
                        "warn", rel, "title-mismatch",
                        f"title `{title}` 与文件名 `{path.stem}` 不一致",
                    )
                if h1_list and title != h1_list[0]:
                    findings.add(
                        "warn", rel, "title-mismatch",
                        f"title `{title}` 与首个 H1 `{h1_list[0]}` 不一致",
                    )

        prev = 0
        for m in HEADING_RE.finditer(body_nofm):
            level = len(m.group(1))
            if prev and level > prev + 1:
                findings.add(
                    "error", rel, "heading-jump",
                    f"标题层级跳级（{prev} 级后直接出现 {level} 级）",
                )
                break
            prev = level

        if rec["fence_unclosed"]:
            findings.add(
                "warn", rel, "fence-unclosed",
                "围栏未闭合，其后内容按代码处理，链接检查已跳过",
            )

        # ---- 双链：报错 + 出链（不含 frontmatter） ----
        count = 0
        for embed, target in iter_links(body_nofm):
            if embed and is_attachment(target):
                if not resolve_attachment(target, file_names, file_rels):
                    findings.add(
                        "error", rel, "broken-embed",
                        f"![[{target}]] 在库内找不到该附件",
                    )
                continue
            hits = resolve_wikilink(target, stem_index, rel_noext, rel_path)
            if not hits:
                findings.add(
                    "error", rel, "broken-wikilink",
                    f"[[{target}]] 解析不到目标",
                )
                continue
            # 重名时 dup-stem 已单独报错，这里只计数不重复报 broken
            if any(h != path for h in hits):
                count += 1
        outbound[path] = count

        # ---- 入链：含 frontmatter（source: [[X]] 也算被链入） ----
        for embed, target in iter_links(body):
            if embed and is_attachment(target):
                continue
            for hit in resolve_wikilink(target, stem_index, rel_noext, rel_path):
                note_inbound(hit, path)

        # ---- 相对 markdown 链接 ----
        for m in MDLINK_RE.finditer(body_nofm):
            target = m.group(1).strip()
            if target.startswith("<") and target.endswith(">"):
                target = target[1:-1].strip()
            if not target or "://" in target or SCHEME_RE.match(target):
                continue
            if target.startswith("#") or "{{" in target:
                continue
            target = url_unquote(target)
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
                # 指向源码仓库，不是本库文件：正常的锚点，不改也不删
                anchors.append((rel, clean))
                continue
            if not exists:
                findings.add(
                    "error", rel, "broken-relative-link",
                    f"失效相对链接 -> {clean}",
                )
                continue
            try:
                hit_rel = resolved.relative_to(vault).as_posix()
            except ValueError:
                continue
            hit = rel_path.get(hit_rel)
            if hit is not None:
                note_inbound(hit, path)

        # ---- 出链数量约束 ----
        in_archive = rel.startswith(EXEMPT_LINK_CHECK_PREFIXES)
        if note_type in {"note", "project"} and not in_archive:
            n = outbound[path]
            if n == 0:
                findings.add(
                    "error", rel, "no-outbound",
                    f"正文零出链（要求 ≥2 条双链，只写 source 不算）",
                )
            elif n == 1:
                findings.add(
                    "warn", rel, "one-outbound",
                    f"正文只有 1 条出链（要求 ≥2 条）",
                )

        # ---- source 字段：[[X]] 必须存在且 type: source ----
        source_raw = unquote(fm.get("source", ""))
        if source_raw.startswith("[[") and source_raw.endswith("]]"):
            src_target = wikilink_target(source_raw[2:-2])
            if src_target:
                hits = resolve_wikilink(src_target, stem_index, rel_noext, rel_path)
                if not hits:
                    findings.add(
                        "error", rel, "source-broken",
                        f"source [[{src_target}]] 解析不到目标",
                    )
                elif not any(
                    unquote(fm_by_path.get(h, {}).get("type", "")) == "source"
                    for h in hits
                ):
                    findings.add(
                        "error", rel, "source-broken",
                        f"source [[{src_target}]] 指向的不是 type: source 的笔记",
                    )

    # --- 双链：全库重名 ---
    for stem in sorted(stem_index):
        paths = stem_index[stem]
        if len(paths) < 2:
            continue
        rels = sorted(p.relative_to(vault).as_posix() for p in paths)
        findings.add(
            "error", rels[0], "dup-stem",
            f"全库重名 `{stem}`（[[{stem}]] 会歧义）: " + ", ".join(rels),
        )

    # --- 孤儿 / evergreen / MOC 索引 / 根目录笔记 / 文件名空格 ---
    for rec in records:
        path = rec["path"]
        rel = rec["rel"]
        fm = rec["fm"]
        sources = inbound_sources.get(path, set())

        if not sources:
            if (
                path.stem not in ORPHAN_EXEMPT_STEMS
                and rel not in ORPHAN_EXEMPT_PATHS
                and not path.stem.endswith("模板")
            ):
                findings.add("warn", rel, "orphan", "孤儿笔记（零入链）")

        if unquote(fm.get("status", "")) == "evergreen" and not sources:
            findings.add(
                "warn", rel, "evergreen-no-inbound",
                "status=evergreen 但没有任何入链",
            )

        note_type = unquote(fm.get("type", ""))
        if note_type in {"note", "project"} and not rel.startswith(
            EXEMPT_LINK_CHECK_PREFIXES
        ):
            moc_inbound = any(
                type_by_path.get(src) == "moc" for src in sources
            )
            if not moc_inbound:
                findings.add(
                    "warn", rel, "not-indexed-by-moc",
                    "没有任何 type: moc 的笔记链入（没有被 MOC 索引到）",
                )

        if "/" not in rel and path.stem not in ROOT_NOTE_EXEMPT_STEMS:
            findings.add(
                "warn", rel, "root-note",
                "笔记直接放在 vault 根目录（应移入 10_Notes/ 等目录）",
            )

        if " " in path.stem:
            findings.add("warn", rel, "filename-space", "文件名含空格")

    return {
        "vault": vault,
        "md_files": md_files,
        "errors": findings.errors,
        "warnings": findings.warnings,
        "code_anchors": sorted(set(anchors)),
    }


def print_human(result, quiet, max_errors):
    errors = result["errors"]
    warnings = result["warnings"]
    anchors = result["code_anchors"]
    print(f"Vault: {result['vault']}")
    print(f"markdown 文件: {len(result['md_files'])}")
    if not quiet:
        for w in warnings:
            print(f"  WARN [{w['rule']}] {w['path']}: {w['message']}")
    print()
    if anchors:
        print(f"代码仓库锚点（指向库外，属正常，不要改也不要删）: {len(anchors)} 处")
        for rel, target in anchors[:3]:
            print(f"  e.g. {rel} -> {target}")
        print()
    if errors:
        print(f"ERROR ({len(errors)}):")
        shown = errors if max_errors <= 0 else errors[:max_errors]
        for e in shown:
            print(f"  ERROR [{e['rule']}] {e['path']}: {e['message']}")
        if len(shown) < len(errors):
            print(
                f"  ... 还有 {len(errors) - len(shown)} 条"
                f"（用 --max-errors N 调整上限）"
            )
        print()
    else:
        print("ERROR (0): 无")
        print()
    print(f"结果: {len(errors)} 个错误 / {len(warnings)} 个警告")


def parse_args(argv):
    """返回 (vault_arg, quiet, as_json, max_errors)；用法错误返回 None。"""
    vault_arg, quiet, as_json, max_errors = None, False, False, 200
    rest = argv[1:]
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg == "--quiet":
            quiet = True
        elif arg == "--json":
            as_json = True
        elif arg == "--max-errors":
            if i + 1 >= len(rest):
                return None
            i += 1
            try:
                max_errors = int(rest[i])
            except ValueError:
                return None
            if max_errors < 1:
                return None
        elif arg.startswith("--max-errors="):
            try:
                max_errors = int(arg.split("=", 1)[1])
            except ValueError:
                return None
            if max_errors < 1:
                return None
        elif arg in ("-h", "--help"):
            return None
        elif arg.startswith("-") and arg != "-":
            return None
        elif vault_arg is None:
            vault_arg = arg
        else:
            return None
        i += 1
    if vault_arg is None:
        return None
    return vault_arg, quiet, as_json, max_errors


def main(argv):
    if any(a in ("-h", "--help") for a in argv[1:]):
        print(__doc__)
        return 0
    parsed = parse_args(argv)
    if parsed is None:
        print(__doc__)
        return 2
    vault_arg, quiet, as_json, max_errors = parsed
    vault = Path(vault_arg).expanduser().resolve()
    if not vault.is_dir():
        # 用法错误：走 stderr，stdout 不产出半截 JSON
        print(f"不是目录: {vault}", file=sys.stderr)
        return 2

    result = scan(vault)
    errors = result["errors"]
    exit_code = 1 if errors else 0

    if as_json:
        payload = {
            "vault": str(result["vault"]),
            "md_files": len(result["md_files"]),
            "errors": errors,
            "warnings": result["warnings"],
            "code_anchors": [
                {"path": rel, "target": target}
                for rel, target in result["code_anchors"]
            ],
            "exit_code": exit_code,
        }
        print(json.dumps(payload, ensure_ascii=False))
        return exit_code

    print_human(result, quiet, max_errors)
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
