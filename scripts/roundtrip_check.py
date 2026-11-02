#!/usr/bin/env python3
"""正文一字未改的机器证明：拆出的笔记必须能逐行反向还原回 30_Sources/ 存档。

背景：SKILL.md 第 5 步用 `git show --stat` 里的 `rename (95%)` 自证正文未变，
但那只证明「原件原样进了存档」，不证明「拆出来的笔记没被改字」——拆出来的
笔记是新增文件，压根不进 rename 统计。这个脚本补的就是这一段。

用法：
    python scripts/roundtrip_check.py <vault_root>
        [--source-dir 30_Sources] [--allow-added "相关,延伸问题"] [--require-full]
        [--json] [--max-report N]

判定规则（只读，不改任何文件）：

  A. 连续子序列（ERROR `rewritten`）
     子笔记去掉标题行/空行/围栏标记后的正文行，必须作为**连续片段**出现在
     存档的同样处理结果里。中间不许跳行，也不许改字。

  B. 标题只降级（ERROR `heading-not-in-source` / `heading-level`）
     子笔记的每个标题文本要能在存档里找到同名标题，且「子 = 存档 − 1」级；
     存档是 1 级时允许子也是 1 级（保留一级标题）。

  C. 覆盖率（默认 WARN，不影响退出码；加 `--require-full` 才是 ERROR）
     存档正文行被所有子笔记覆盖的比例；未覆盖行列出来由人判断。**默认不因
     未覆盖判失败，因为存档保留全书**——只拆其中一部分是合法用法，所以默认
     只打 WARN，退出码保持 0。要严格比对（有子笔记的存档必须逐行全覆盖）
     就加 `--require-full`：任何未覆盖的 core line 都判 ERROR 并 exit 1。
     没有任何子笔记的存档不受严格模式约束（还没拆，不算漏）。

  D. `source` 指向不存在的笔记 → ERROR `source-broken`。

退出码：0 = 全部通过，1 = 发现差异/丢失（或 `--require-full` 下有未覆盖行），
2 = 用法错误 / 内置自检失败。

JSON（`--json` 时 stdout 只有 JSON）：`vault` / `sources` / `issues` /
`unlinked_notes` / `require_full` / `uncovered_lines` / `exit_code`。
`require_full` 是本次是否开了严格覆盖；`uncovered_lines` 是「有子笔记的
存档」里未覆盖正文行的总数（没有未覆盖则为 0），也是 `--require-full`
判失败的依据。每个 source 有 `path` / `core_lines` / `children` /
`coverage`；每个 child 有 `path` / `ok` / `match` / `issues`；每个 issue 有
`rule` / `detail` / `archive_line` / `note_line` / `archive_text` / `note_text`。

行号语义（别混用）：`rewritten` 的 `archive_line` / `note_line` 是**正文行
序号**（1 起，标题与空行不算），负数/None 表示对不到；标题类 issue 的
`note_line` 是标题在笔记文件里的**物理行号**，`archive_line` 为 null。

已知取舍（也写在汇报里）：
- 标题比对先试「去前导编号」（`1. ` / `第3章` / `(2)`）与首尾冒号后的结果，
  再试原始文本，任一相等即通过，所以 `# 会话持久化` 能对上存档的
  `## 1. 会话持久化`；除此之外必须同名。归一化把整段吃空时（如 `第三节`
  整句就是编号）回退用原始标题，绝不拿空串去比。
- `第 3 节` 与 `第三 节` 这类纯数字/中文数字写法差异不要求判等（会报
  `heading-not-in-source`，但报错信息里显示真实标题）。
- 只匹配第一处完全相同的连续片段；正文里有大段完全重复的章节时可能定位到
  较早的那一处（内容等价，行号可能偏前）。
- 子笔记正文为空（只有标题 / 只有 `--allow-added` 小节）判 `rewritten` 失败，
  因为「拆分整段丢失」正是这个样子。
"""

import argparse
import difflib
import json
import re
import sys
import unicodedata
from pathlib import Path

# 与 validate_vault.py 保持一致：这些目录不是笔记
SKIP_DIR_PARTS = {".git", ".obsidian", "node_modules", ".trash", "_templates"}
DEFAULT_ALLOW_ADDED = ("相关", "延伸问题")

FRONTMATTER_RE = re.compile(r"\A\ufeff?---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})")
HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*$")
TRAILING_HASH_RE = re.compile(r"[ \t]+#+[ \t]*$")
NUMBERING_RE = re.compile(
    r"^(?:"
    r"\d+[.)、）]"
    r"|[（(]\d+[)）]"
    r"|第[0-9０-９一二三四五六七八九十百千]+[章节部分]"
    r"|chapter\s+\d+"
    r")[ \t]*",
    re.I,
)
WIKILINK_RE = re.compile(r"!?\[\[([^\]]+)\]\]")
WS_RE = re.compile(r"\s+")


# ---------------------------------------------------------------- 基础工具

def read_text(path):
    return path.read_text(encoding="utf-8-sig", errors="replace")


def display_width(text):
    """东亚宽字符按 2 列算，保证中文路径也能对齐。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text, width):
    return text + " " * max(0, width - display_width(text))


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


def strip_frontmatter(text):
    return FRONTMATTER_RE.sub("", text, count=1)


def unquote(raw):
    if raw is None:
        return None
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    if not value or value.lower() in {"null", "none", "~", "nil"}:
        return None
    return value


URL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")


def wikilink_target(raw):
    target = raw.split("|", 1)[0].split("#", 1)[0].strip()
    if target.endswith("\\"):
        target = target[:-1].strip()
    return target


def source_target(fm):
    """`source: "[[原文]]"` / `source: [[原文]]` / `source: 原文` → 目标名。"""
    if not fm:
        return None
    value = unquote(fm.get("source"))
    if not value:
        return None
    m = WIKILINK_RE.search(value)
    if m:
        return wikilink_target(m.group(1)) or None
    if "{{" in value or "\n" in value:
        return None
    # 外部 URL 不是双链，别当成笔记名去解析（否则 https://... 会被报 source-broken）
    if URL_RE.match(value):
        return None
    return value


def norm_heading(text):
    """标题比对用的归一化：去围栏尾 #、去前导编号、去首尾冒号、压空白。

    归一化把整段吃空时（`第三节` / `第3节` 整个标题就是编号）回退用原始
    标题——绝不返回空串，否则报错信息里是空白，比对也失衡。
    """
    raw = WS_RE.sub(" ", (text or "").strip())
    value = TRAILING_HASH_RE.sub("", raw).strip()
    value = NUMBERING_RE.sub("", value).strip()
    value = value.strip(":：").strip()
    value = WS_RE.sub(" ", value)
    return value or raw.strip(":：").strip() or raw


def heading_keys(text):
    """标题同名的判定键：归一化结果 + 原始文本，去空去重。

    任一相等即视为同名。归一化已保证非空（回退原文），这里再挡一次空串。
    """
    keys = []
    for key in (norm_heading(text), WS_RE.sub(" ", (text or "").strip())):
        if key and key not in keys:
            keys.append(key)
    return keys


def self_check():
    """内置自检：标题归一化不能把纯编号标题吃成空串。

    历史 bug：`第三节` / `第3节` 被 NUMBERING_RE 整段吃掉返回 ''，
    于是子笔记 `# 第三节` 与存档比对时报「存档里没有同名标题（去编号后
    比对 ``）」，信息是空白。返回失败说明列表（空列表 = 通过）。
    """
    cases = (
        ("第三节", "第三节"),
        ("第3节", "第3节"),
        ("第三 节", "第三 节"),
        ("第 3 节", "第 3 节"),
        ("第三节:", "第三节"),
        ("3. 冒泡排序", "冒泡排序"),
        ("（2）快速排序", "快速排序"),
        ("冒泡排序", "冒泡排序"),
    )
    failures = []
    for raw, want in cases:
        got = norm_heading(raw)
        if got != want:
            failures.append(f"norm_heading({raw!r}) == {got!r}，期望 {want!r}")
    for raw in ("第三节", "第3节", "第 3 节", "第三节:"):
        if not norm_heading(raw):
            failures.append(f"norm_heading({raw!r}) 返回空串（应回退原始标题）")
    if not heading_keys("第三节"):
        failures.append("heading_keys('第三节') 为空（标题比对键丢失）")
    return failures


def level_ok(note_level, archive_level):
    """只许降一级；存档是 1 级时允许保留 1 级。"""
    if archive_level <= 1:
        return note_level == 1
    return note_level == archive_level - 1


def is_inside(path, root):
    return path == root or root in path.parents


def rel_path(vault, path):
    return path.relative_to(vault).as_posix()


# ---------------------------------------------------------------- 正文抽取

def tokenize(lines):
    """把正文切成 token：(kind, lineno, level, text)。

    kind: fence（围栏标记行，丢弃）/ code（围栏内内容，保留）/ heading /
          text / blank（丢弃）。
    围栏里的 `# 注释` 不会被误当成标题。
    """
    tokens = []
    fence = None
    for lineno, raw in enumerate(lines, 1):
        if not raw.strip():
            tokens.append(("blank", lineno, 0, raw))
            continue
        m = FENCE_RE.match(raw)
        if m:
            marker = m.group(1)
            if fence is None:
                fence = marker[0]
            elif marker[0] == fence:
                fence = None
            else:
                # 另一种围栏符在代码内出现，按内容处理
                tokens.append(("code", lineno, 0, raw))
                continue
            tokens.append(("fence", lineno, 0, raw))
            continue
        if fence is not None:
            tokens.append(("code", lineno, 0, raw))
            continue
        hm = HEADING_RE.match(raw)
        if hm:
            level = len(hm.group(1))
            text = TRAILING_HASH_RE.sub("", hm.group(2)).strip()
            tokens.append(("heading", lineno, level, text))
            continue
        tokens.append(("text", lineno, 0, raw))
    return tokens


def extract(tokens, allow_added=()):
    """返回 (core_lines, headings=[(level, text, lineno)])。

    先整段剔除 --allow-added 里的顶层小节，再丢掉标题行/空行/围栏标记行。
    围栏内的内容行保留。
    """
    allowed = {norm_heading(name) for name in allow_added if norm_heading(name)}
    keep = [True] * len(tokens)
    if allowed:
        for i, tok in enumerate(tokens):
            if tok[0] != "heading" or norm_heading(tok[3]) not in allowed:
                continue
            level = tok[2]
            j = i
            while j < len(tokens):
                cur = tokens[j]
                if j > i and cur[0] == "heading" and cur[2] <= level:
                    break
                keep[j] = False
                j += 1

    core, headings = [], []
    for tok, kept in zip(tokens, keep):
        if not kept:
            continue
        if tok[0] in ("text", "code"):
            core.append(tok[3])
        elif tok[0] == "heading":
            headings.append((tok[2], tok[3], tok[1]))
    return core, headings


# ---------------------------------------------------------------- 连续匹配

def find_contiguous(archive, note):
    """note 是否为 archive 的连续片段。返回 (start, end_exclusive)；否则 None。"""
    if not note:
        return (0, 0)
    positions = {}
    for index, line in enumerate(archive):
        positions.setdefault(line, []).append(index)
    for start in positions.get(note[0], ()):
        if start + len(note) <= len(archive) and archive[start:start + len(note)] == note:
            return (start, start + len(note))
    return None


def best_alignment(archive, note):
    """找不到连续匹配时，猜一个最像的落点：(start, matched_prefix, block)。"""
    start, prefix = 0, 0
    if note:
        positions = {}
        for index, line in enumerate(archive):
            positions.setdefault(line, []).append(index)
        for cand in positions.get(note[0], ()):
            limit = min(len(note), len(archive) - cand)
            k = 0
            while k < limit and note[k] == archive[cand + k]:
                k += 1
            if k > prefix:
                start, prefix = cand, k
                if k == len(note):
                    break

    sm = difflib.SequenceMatcher(None, archive, note, autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size]
    block = max(blocks, key=lambda b: b.size) if blocks else None

    if prefix == 0 and block is not None:
        start = max(block.a - block.b, 0)
        limit = min(len(note), len(archive) - start)
        k = 0
        while k < limit and note[k] == archive[start + k]:
            k += 1
        prefix = k
    return start, prefix, block


def rewritten_issue(archive, note):
    start, prefix, block = best_alignment(archive, note)
    if block is not None:
        where = (
            f"最接近的匹配块：存档第 {block.a + 1}-{block.a + block.size} 行"
            f" ↔ 笔记第 {block.b + 1}-{block.b + block.size} 行"
            f"（{block.size} 行）"
        )
    else:
        where = "笔记正文在存档中找不到任何能对齐的行"

    a_index = start + prefix
    if a_index < len(archive):
        archive_line = a_index + 1
        archive_text = archive[a_index]
    else:
        # 笔记在存档正文结束之后还有内容：存档已经比完，没有对应行
        archive_line = None
        archive_text = "（存档正文已结束，无对应行）"
    note_text = note[prefix] if prefix < len(note) else ""
    if archive_line is None:
        detail_tail = where + "；笔记在存档正文末尾之后还有多余内容"
    else:
        detail_tail = where
    return {
        "rule": "rewritten",
        "detail": f"正文不是存档的连续片段（中间跳行、改写或增删）：{detail_tail}",
        "archive_line": archive_line,
        "note_line": prefix + 1 if note_text else None,
        "archive_text": archive_text,
        "note_text": note_text,
    }


# ---------------------------------------------------------------- 标题断言

def check_headings(note_headings, archive_headings):
    issues = []
    index = {}
    for level, text, _lineno in archive_headings:
        for key in heading_keys(text):
            index.setdefault(key, []).append((level, text))

    for level, text, lineno in note_headings:
        keys = heading_keys(text)
        note_md = f"{'#' * level} {text}".strip()
        candidates = []
        for key in keys:
            for item in index.get(key, ()):
                if item not in candidates:
                    candidates.append(item)
        if not candidates:
            # 归一化为空时 norm_heading 已回退原文，这里 keys 不会含空串；
            # 报错信息里必须是真实标题，不能是 ``
            shown = "、".join(f"`{key}`" for key in keys) or f"`{text.strip()}`"
            issues.append({
                "rule": "heading-not-in-source",
                "detail": f"存档里没有同名标题（笔记 `{note_md}`，比对用 {shown}）",
                "archive_line": None,
                "note_line": lineno,
                "archive_text": "",
                "note_text": note_md,
            })
            continue
        if any(level_ok(level, a_level) for a_level, _ in candidates):
            continue
        a_level, a_text = candidates[0]
        want = 1 if a_level <= 1 else a_level - 1
        archive_md = f"{'#' * a_level} {a_text}"
        issues.append({
            "rule": "heading-level",
            "detail": (
                f"标题只许降 1 级：存档 `{archive_md}` 为 {a_level} 级，"
                f"笔记 `{note_md}` 为 {level} 级，应为 {want} 级"
                f"（`{'#' * want} {text}`）"
            ),
            "archive_line": None,
            "note_line": lineno,
            "archive_text": archive_md,
            "note_text": note_md,
        })
    return issues


# ---------------------------------------------------------------- 索引与解析

def iter_md(vault):
    found = []
    for path in vault.rglob("*.md"):
        if not path.is_file():  # 名字像笔记的目录（foo.md/）不算
            continue
        try:
            parts = path.relative_to(vault).parts
        except ValueError:
            continue
        if any(part in SKIP_DIR_PARTS for part in parts):
            continue
        if any(part.startswith(".") for part in parts):
            continue
        found.append(path)
    return sorted(found, key=lambda p: p.relative_to(vault).as_posix())


def load_notes(vault):
    notes = []
    for path in iter_md(vault):
        try:
            raw = read_text(path)
        except OSError:
            continue  # 读不了的文件不阻断其余比对
        fm = parse_frontmatter(raw)
        if fm is None:
            continue  # 没有 frontmatter 的文件（如根 README）不是笔记，别计入统计
        note_type = (unquote(fm.get("type")) or "").lower()
        notes.append({
            "path": path,
            "rel": rel_path(vault, path),
            "type": note_type,
            "source": source_target(fm),
            "tokens": tokenize(strip_frontmatter(raw).splitlines()),
        })
    return notes


def resolve_target(target, by_stem, source_dir):
    """basename → 路径；同名多篇时优先 source_dir 里、路径最短的。"""
    for name in (target, Path(target).stem):
        candidates = by_stem.get(name)
        if candidates:
            def key(path):
                inside = 0 if (source_dir and is_inside(path, source_dir)) else 1
                return (inside, len(path.parts), path.as_posix())

            return sorted(candidates, key=key)[0]
    return None


# ---------------------------------------------------------------- 主检查

def check(vault, source_dir, allow_added, max_report, require_full=False):
    notes = load_notes(vault)
    by_stem = {}
    for note in notes:
        by_stem.setdefault(note["path"].stem, []).append(note["path"])

    archives = [n for n in notes if n["type"] == "source"]
    if source_dir is not None:
        archives = [n for n in archives if is_inside(n["path"], source_dir)]
    archives.sort(key=lambda n: n["rel"])

    children = {n["path"]: [] for n in archives}
    broken = []
    unlinked = 0

    for note in notes:
        target = note["source"]
        if not target:
            if note["type"] != "source":
                unlinked += 1
            continue
        resolved = resolve_target(target, by_stem, source_dir)
        if resolved is None:
            broken.append((note["rel"], target, "missing"))
            continue
        if resolved == note["path"]:
            broken.append((note["rel"], target, "self"))
            continue
        if resolved in children:
            children[resolved].append(note["path"])
        else:
            # 文件确实存在，但不是本次要核对的 type: source 存档
            # （type 写错、或存档放在 --source-dir 之外）：不能静默跳过，
            # 否则这类笔记会从报告里彻底消失
            broken.append((note["rel"], target, "not-archive"))

    sources_out = []
    passed = failed = 0
    # --require-full 的判定范围：有子笔记的存档里未覆盖的正文行总数
    strict_gaps = []

    for archive in archives:
        a_core, a_headings = extract(archive["tokens"], ())
        kids = [n for n in notes if n["path"] in set(children[archive["path"]])]
        kids.sort(key=lambda n: n["rel"])

        covered = set()
        child_entries = []

        for kid in kids:
            n_core, n_headings = extract(kid["tokens"], allow_added)
            match = find_contiguous(a_core, n_core)

            issues = []
            if not n_core:
                # 空正文不能算通过：拆分整段丢失时正是这个样子
                issues.append({
                    "rule": "rewritten",
                    "detail": "子笔记没有任何正文行（去掉标题/空行/围栏标记后为空），"
                              "拆分可能整段丢失",
                    "archive_line": None,
                    "note_line": None,
                    "archive_text": "",
                    "note_text": "",
                })
            elif match is None:
                issues.append(rewritten_issue(a_core, n_core))
            issues.extend(check_headings(n_headings, a_headings))

            ok = not issues
            if ok:
                passed += 1
            else:
                failed += 1

            if match is not None and n_core:
                covered.update(range(match[0], match[1]))
                match_json = {"start": match[0] + 1, "end": match[1]}
            elif match is not None:
                match_json = {"start": 0, "end": 0}
            else:
                match_json = None

            child_entries.append({
                "path": kid["rel"],
                "ok": ok,
                "match": match_json,
                "issues": issues,
            })

        total = len(a_core)
        uncovered = [i for i in range(total) if i not in covered]
        ratio = round(len(covered) / total, 3) if total else 1.0
        missing = total - len(covered)
        if kids and missing:
            # 有子笔记却只覆盖了一部分：默认只是 WARN，--require-full 才算失败
            strict_gaps.append((archive["rel"], missing))

        sources_out.append({
            "path": archive["rel"],
            "core_lines": total,
            "children": child_entries,
            "coverage": {
                "covered": len(covered),
                "total": total,
                "ratio": ratio,
                "uncovered": [
                    f"第 {i + 1} 行: {a_core[i]}" for i in uncovered[:max_report]
                ],
            },
        })

    detail_tpl = {
        "missing": "source 指向不存在的笔记 [[{target}]]",
        "self": "source 指向笔记自己 [[{target}]]（应为上游存档）",
        "not-archive": (
            "source 指向的 [[{target}]] 不是本次核对的 type: source 存档"
            "（type 是否写错？存档在别的目录时用 --source-dir 指过去）"
        ),
    }
    broken_issues = [{
        "rule": "source-broken",
        "path": note_rel,
        "detail": detail_tpl[kind].format(target=target),
        "archive_line": None,
        "note_line": None,
        "archive_text": "",
        "note_text": "",
    } for note_rel, target, kind in broken]

    uncovered_lines = sum(missing for _rel, missing in strict_gaps)
    require_full_failed = bool(require_full and strict_gaps)
    exit_code = 0 if (
        failed == 0 and not broken_issues and not require_full_failed
    ) else 1
    return {
        "vault": str(vault),
        "sources": sources_out,
        "issues": broken_issues,
        "unlinked_notes": unlinked,
        "require_full": bool(require_full),
        "uncovered_lines": uncovered_lines,
        "exit_code": exit_code,
    }


# ---------------------------------------------------------------- 人类可读输出

def status_text(child):
    issues = child["issues"]
    rewritten = next((i for i in issues if i["rule"] == "rewritten"), None)
    if rewritten is not None:
        line = rewritten.get("archive_line")
        if line is None:
            if not rewritten.get("note_text"):
                return "笔记没有任何正文行"
            return "笔记末尾多出正文（存档正文已比完）"
        return f"存档第 {line} 行起不匹配"

    match = child.get("match") or {}
    start, end = match.get("start") or 0, match.get("end") or 0
    if not start:
        base = "笔记无正文行"
    elif start == end:
        base = f"连续匹配 @ 存档第 {start} 行"
    else:
        base = f"连续匹配 @ 存档第 {start}-{end} 行"
    if issues:
        return base + "，但标题有问题"
    return base


def render(report, max_report, allow_added=DEFAULT_ALLOW_ADDED):
    out = []
    if tuple(allow_added) != DEFAULT_ALLOW_ADDED:
        shown = "、".join(allow_added) if allow_added else "（空）"
        out.append(f"新增小节白名单: {shown}")
        out.append("")
    for index, source in enumerate(report["sources"]):
        if index:
            out.append("")
        total = source["core_lines"]
        coverage = source["coverage"]
        kids = source["children"]
        pct = (coverage["covered"] / total * 100) if total else 100.0

        out.append(f"存档: {source['path']} ({total} core lines)")
        out.append(
            f"  子笔记 {len(kids)} 篇，覆盖 {coverage['covered']}/{total} 行 ({pct:.1f}%)"
        )

        width = max([display_width(k["path"]) for k in kids] + [24])
        width = min(width + 2, 56)

        for child in kids:
            tag = "[OK]" if child["ok"] else "[FAIL]"
            out.append("  " + tag.ljust(7) + pad(child["path"], width) + status_text(child))

            for issue in child["issues"]:
                if issue["rule"] == "rewritten":
                    if issue["archive_text"] or issue["note_text"]:
                        out.append(f" +    原文: {issue['archive_text']}")
                        out.append(f" -    笔记: {issue['note_text']}")

            shown = 0
            for issue in child["issues"]:
                if issue["rule"] == "rewritten":
                    continue
                if shown >= max_report:
                    break
                out.append(f"      [!] {issue['rule']}: {issue['detail']}")
                shown += 1
            rest = len([i for i in child["issues"] if i["rule"] != "rewritten"]) - shown
            if rest > 0:
                out.append(f"      [!] … 还有 {rest} 条标题问题未列出")

        missing = total - coverage["covered"]
        if missing:
            strict = bool(report.get("require_full")) and bool(kids)
            reason = (
                "--require-full 要求有子笔记的存档逐行全覆盖"
                if strict
                else "存档保留全书、允许只拆部分，请自行判断"
            )
            out.append(f"  未覆盖 {missing} 行（{'ERROR' if strict else 'WARN'}：{reason}）:")
            for entry in coverage["uncovered"]:
                out.append(f"    - {entry}")
            if missing > len(coverage["uncovered"]):
                out.append(f"    - … 还有 {missing - len(coverage['uncovered'])} 行未列出")

    if not report["sources"]:
        out.append("没有找到 type: source 的存档笔记（可用 --source-dir 指定存档目录）")

    if report["issues"]:
        out.append("")
        out.append(f"source 失效（ERROR）: {len(report['issues'])} 篇")
        for issue in report["issues"]:
            out.append(f"  [!] {issue['rule']}: {issue['path']} → {issue['detail']}")

    passed = sum(1 for s in report["sources"] for c in s["children"] if c["ok"])
    failed = sum(1 for s in report["sources"] for c in s["children"] if not c["ok"])
    out.append("")
    out.append(f"未关联存档的笔记: {report['unlinked_notes']} 篇")
    if report.get("require_full") and report.get("uncovered_lines"):
        out.append(
            f"严格覆盖（--require-full）: 有子笔记的存档仍有 "
            f"{report['uncovered_lines']} 行未覆盖 → 判失败"
        )
    out.append(f"结果: {passed} 通过 / {failed} 失败")
    return "\n".join(out)


# ---------------------------------------------------------------- CLI

def build_parser():
    parser = argparse.ArgumentParser(
        prog="roundtrip_check.py",
        description="校验拆出的笔记能否逐行反向还原回 30_Sources/ 存档（只读）。",
    )
    parser.add_argument("vault_root", help="vault 根目录")
    parser.add_argument(
        "--source-dir", default=None,
        help="存档所在目录，相对 vault 或绝对路径（默认 30_Sources，不存在则扫全库）",
    )
    parser.add_argument(
        "--allow-added", action="append", default=None, metavar="NAMES",
        help='在默认 "相关,延伸问题" 之外再允许的新增小节名，逗号分隔；'
             '传空串表示一个都不允许（这些小节连同内容不参与比对）',
    )
    parser.add_argument("--json", action="store_true", help="stdout 只输出 JSON")
    parser.add_argument(
        "--require-full", action="store_true",
        help="严格覆盖：有子笔记的存档必须被完全覆盖，任何未覆盖正文行都判 ERROR "
             "并 exit 1（默认不因未覆盖判失败，因为存档保留全书、允许只拆一部分）",
    )
    parser.add_argument(
        "--max-report", type=int, default=50, metavar="N",
        help="差异/未覆盖行最多列 N 条（默认 50）",
    )
    return parser


def parse_allow_added(values):
    """--allow-added 是「在默认白名单之外再加」，方便不同 vault 有自己的新增小节习惯。
    显式传空串则清空整个白名单（一个新增小节都不放过）。
    """
    if values is None:
        return DEFAULT_ALLOW_ADDED
    parts = [
        part.strip()
        for value in values
        for part in value.replace("，", ",").split(",")
    ]
    if not any(parts):
        return ()
    names = list(DEFAULT_ALLOW_ADDED)
    for part in parts:
        if part and part not in names:
            names.append(part)
    return tuple(names)


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", newline="\n")
        except (AttributeError, ValueError):
            pass

    # 内置自检：标题归一化不能把纯编号标题吃成空串（否则报错信息是空白）
    failures = self_check()
    if failures:
        for line in failures:
            print(f"内置自检失败: {line}", file=sys.stderr)
        return 2

    vault = Path(args.vault_root).expanduser()
    if not vault.is_dir():
        print(f"不是目录: {args.vault_root}", file=sys.stderr)
        return 2
    vault = vault.resolve()

    if args.max_report < 0:
        print("--max-report 不能为负数", file=sys.stderr)
        return 2

    source_dir = None
    if args.source_dir is not None:
        candidate = Path(args.source_dir).expanduser()
        if not candidate.is_absolute():
            candidate = vault / candidate
        if not candidate.is_dir():
            print(f"source 目录不存在: {args.source_dir}", file=sys.stderr)
            return 2
        source_dir = candidate.resolve()
    else:
        default_dir = vault / "30_Sources"
        if default_dir.is_dir():
            source_dir = default_dir.resolve()

    allow_added = parse_allow_added(args.allow_added)
    report = check(
        vault=vault,
        source_dir=source_dir,
        allow_added=allow_added,
        max_report=args.max_report,
        require_full=args.require_full,
    )

    if args.json:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render(report, args.max_report, allow_added) + "\n")
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
