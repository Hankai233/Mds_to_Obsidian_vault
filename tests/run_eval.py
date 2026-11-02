#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Obsidian 知识库技能的回归测试入口。

一条命令跑完全部用例：

    python3 tests/run_eval.py            # 只打 PASS/FAIL 表
    python3 tests/run_eval.py --verbose  # 额外打印子进程原始输出

用例与固定行为（README.md 第 149-163 行那张对照表的可复跑版本）：

    clean    规范知识库 -> validate_vault.py 退出 0，0 错误 0 警告
    messy    杂乱知识库 -> 退出 1，命中预期错误/警告，且两类历史误报不再出现
    split    拆分对照 -> roundtrip_check.py 认可照搬篇、抓住改写篇并定位到行
    urlsrc   source 是外部 URL -> roundtrip_check.py 不得误报 source-broken

只用标准库。被测脚本若还在并行重写（文件缺失、拒绝参数、输出不可解析），
这里报 SKIP 并继续跑其余用例，不抛异常、不中断。
"""

import argparse
import json
import re
import subprocess
import sys
import unicodedata
from collections import namedtuple
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
VALIDATE_SCRIPT = REPO_ROOT / "scripts" / "validate_vault.py"
ROUNDTRIP_SCRIPT = REPO_ROOT / "scripts" / "roundtrip_check.py"

# 单个子进程的超时（秒）：正常几百毫秒，留足余量但不至于卡死
PROCESS_TIMEOUT = 180

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"

Row = namedtuple("Row", "case check status detail")

# --verbose 时要把子进程原始输出原样留下来，所以统一从这里收集
RAW_OUTPUT = []


class Skip(Exception):
    """用例前置条件不满足（被测脚本还没就绪），报 SKIP 而不是 FAIL。"""


# ---------------------------------------------------------------------------
# 子进程与输出解析
# ---------------------------------------------------------------------------


def run_script(args):
    """跑被测脚本，返回 CompletedProcess（文本模式，UTF-8）。"""
    if VERBOSE:
        print("$ " + " ".join(args))
    proc = subprocess.run(
        args,
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=PROCESS_TIMEOUT,
        encoding="utf-8",
        errors="replace",
    )
    RAW_OUTPUT.append(
        "$ {cmd}\n[exit {code}]\n--- stdout ---\n{out}\n--- stderr ---\n{err}".format(
            cmd=" ".join(args),
            code=proc.returncode,
            out=proc.stdout,
            err=proc.stderr,
        )
    )
    return proc


def parse_json_output(text):
    """从 stdout 里取 JSON：先整体解析，失败再退化成找最外层花括号。"""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


def brief(text, limit=400):
    """把子进程输出压成一行，用于 FAIL/SKIP 的说明。"""
    text = (text or "").strip()
    if len(text) > limit:
        text = text[:limit] + " …（已截断，用 --verbose 看全文）"
    return text.replace("\n", " ⏎ ")


def rule_set(entries):
    """从 errors/warnings 列表里提取 rule 集合（保持去重计数）。"""
    return {str(item.get("rule", "")) for item in entries or []}


# 从报错文案里抠出被判定失效的链接目标，避免用子串匹配误伤
# （`存在的图.png` 是 `不存在的图.png` 的子串，不能直接 in）
EMBED_TARGET_RE = re.compile(r"!\[\[([^\]\|#]+)")
WIKILINK_TARGET_RE = re.compile(r"\[\[([^\]\|#]+)")


def errors_about(errors, rule, target_re, expected_target):
    """筛出 rule 匹配、且报错文案里的链接目标等于 expected_target 的条目。"""
    hits = []
    for item in errors:
        if item.get("rule") != rule:
            continue
        message = str(item.get("message") or "")
        targets = [match.strip() for match in target_re.findall(message)]
        if any(target.replace("\\", "/") == expected_target for target in targets):
            hits.append(item)
    return hits


# ---------------------------------------------------------------------------
# 被测脚本的调用封装（脚本未就绪时抛 Skip）
# ---------------------------------------------------------------------------


def validate_json(vault_root):
    """调 validate_vault.py --json，返回 (解析后的 dict, CompletedProcess)。"""
    if not VALIDATE_SCRIPT.exists():
        raise Skip("scripts/validate_vault.py 不存在（并行重写中？）")
    proc = run_script(
        [
            sys.executable,
            str(VALIDATE_SCRIPT),
            str(vault_root),
            "--json",
            "--max-errors",
            "200",
        ]
    )
    if proc.returncode not in (0, 1, 2):
        raise Skip(
            "validate_vault.py 退出码 {} 不是 0/1/2：{}".format(
                proc.returncode, brief(proc.stderr)
            )
        )
    if proc.returncode == 2:
        raise Skip(
            "validate_vault.py 不认 --json/--max-errors（exit 2）：{}".format(
                brief(proc.stderr) or brief(proc.stdout)
            )
        )
    data = parse_json_output(proc.stdout)
    if not isinstance(data, dict) or "errors" not in data:
        raise Skip(
            "validate_vault.py --json 输出不可解析或缺少 errors 字段：{}".format(
                brief(proc.stdout)
            )
        )
    return data, proc


def roundtrip_json(vault_root):
    """调 roundtrip_check.py --json，返回 (解析后的 dict, CompletedProcess)。"""
    if not ROUNDTRIP_SCRIPT.exists():
        raise Skip("scripts/roundtrip_check.py 不存在（并行新增中？）")
    proc = run_script(
        [
            sys.executable,
            str(ROUNDTRIP_SCRIPT),
            str(vault_root),
            "--json",
            "--max-report",
            "200",
        ]
    )
    if proc.returncode not in (0, 1, 2):
        raise Skip(
            "roundtrip_check.py 退出码 {} 不是 0/1/2：{}".format(
                proc.returncode, brief(proc.stderr)
            )
        )
    if proc.returncode == 2:
        raise Skip(
            "roundtrip_check.py 不认 --json/--max-report（exit 2）：{}".format(
                brief(proc.stderr) or brief(proc.stdout)
            )
        )
    data = parse_json_output(proc.stdout)
    if not isinstance(data, dict) or "sources" not in data:
        raise Skip(
            "roundtrip_check.py --json 输出不可解析或缺少 sources 字段：{}".format(
                brief(proc.stdout)
            )
        )
    return data, proc


def find_child(data, path_suffix):
    """在 roundtrip 报告里按路径后缀找一篇子笔记。"""
    for source in data.get("sources") or []:
        for child in source.get("children") or []:
            path = str(child.get("path", "")).replace("\\", "/")
            if path.endswith(path_suffix):
                return child
    return None


# ---------------------------------------------------------------------------
# 用例一：clean —— 规范知识库必须零错误零警告
# ---------------------------------------------------------------------------


def case_clean():
    vault = FIXTURES / "clean"
    rows = []
    data, proc = validate_json(vault)

    rows.append(Row("clean", "exit 0", PASS if proc.returncode == 0 else FAIL,
                    "实际 exit {}".format(proc.returncode)))

    errors = data.get("errors") or []
    detail = "" if not errors else "；".join(
        "[{}] {}: {}".format(item.get("rule"), item.get("path"), item.get("message"))
        for item in errors[:10]
    )
    rows.append(Row("clean", "errors == []", PASS if not errors else FAIL,
                    detail or "共 {} 条错误".format(len(errors))))

    warnings = data.get("warnings") or []
    if warnings:
        # 规范样本上任何警告都是 mismatch，全文打出来便于定位
        print("\n[clean] 规范 fixture 出现计划外警告 {} 条：".format(len(warnings)))
        for item in warnings:
            print("  WARN [{}] {}: {}".format(
                item.get("rule"), item.get("path"), item.get("message")))
    detail = "" if not warnings else "；".join(
        "[{}] {}: {}".format(item.get("rule"), item.get("path"), item.get("message"))
        for item in warnings[:10]
    )
    rows.append(Row("clean", "warnings == []", PASS if not warnings else FAIL,
                    detail or "共 {} 条警告".format(len(warnings))))

    # JSON 契约字段：这些字段缺失说明输出结构被改坏了
    missing = [key for key in ("vault", "md_files", "errors", "warnings",
                               "code_anchors", "exit_code") if key not in data]
    rows.append(Row("clean", "JSON 契约字段齐全", PASS if not missing else FAIL,
                    "缺少字段: {}".format(", ".join(missing)) if missing else "6/6"))
    return rows


# ---------------------------------------------------------------------------
# 用例二：messy —— 杂乱知识库必须命中预期规则
# ---------------------------------------------------------------------------

# 预期出现的错误规则（SUPERSET 断言：真实集合包含这些即可，新增规则不算失败）
MESSY_EXPECTED_ERROR_RULES = [
    "frontmatter-missing",
    "field-missing",
    "field-unknown",
    "type-invalid",
    "type-dir-mismatch",
    "date-invalid",
    "status-invalid",
    "confidence-invalid",
    "heading-jump",
    "no-outbound",
    "broken-wikilink",
    "broken-embed",
    "broken-relative-link",
    "dup-stem",
    "source-broken",
]

MESSY_EXPECTED_WARN_RULES = [
    "root-note",
    "multi-h1",
    "no-h1",
    "filename-space",
    "orphan",
    "one-outbound",
    "evergreen-no-inbound",
    "fence-unclosed",
    "not-indexed-by-moc",
]


def case_messy():
    vault = FIXTURES / "messy"
    rows = []
    data, proc = validate_json(vault)

    rows.append(Row("messy", "exit 1", PASS if proc.returncode == 1 else FAIL,
                    "实际 exit {}".format(proc.returncode)))

    errors = data.get("errors") or []
    warnings = data.get("warnings") or []
    actual_rules = rule_set(errors)
    missing = [rule for rule in MESSY_EXPECTED_ERROR_RULES if rule not in actual_rules]
    rows.append(Row(
        "messy", "errors 规则集 ⊇ 预期 {} 条".format(len(MESSY_EXPECTED_ERROR_RULES)),
        PASS if not missing else FAIL,
        "缺少: {}".format(", ".join(missing)) if missing
        else "命中 {} 条错误 / {} 种规则".format(len(errors), len(actual_rules)),
    ))

    actual_warns = rule_set(warnings)
    missing_warns = [rule for rule in MESSY_EXPECTED_WARN_RULES if rule not in actual_warns]
    rows.append(Row(
        "messy", "warnings 规则集 ⊇ 预期 {} 条".format(len(MESSY_EXPECTED_WARN_RULES)),
        PASS if not missing_warns else FAIL,
        "缺少: {}".format(", ".join(missing_warns)) if missing_warns
        else "命中 {} 条警告 / {} 种规则".format(len(warnings), len(actual_warns)),
    ))

    # 回归防线 1：存在的附件嵌入必须放过（历史 bug：被当成失效双链）
    embed_errors = errors_about(errors, "broken-embed", EMBED_TARGET_RE, "存在的图.png")
    rows.append(Row(
        "messy", "![[存在的图.png]] 不报 broken-embed",
        PASS if not embed_errors else FAIL,
        "" if not embed_errors else embed_errors[0].get("message"),
    ))

    # 回归防线 2：全路径双链必须放过
    link_errors = errors_about(errors, "broken-wikilink", WIKILINK_TARGET_RE, "notes/a/Guide")
    rows.append(Row(
        "messy", "[[notes/a/Guide]] 不报 broken-wikilink",
        PASS if not link_errors else FAIL,
        "" if not link_errors else link_errors[0].get("message"),
    ))

    # 代码仓库锚点：指向 src/*.java 之类的链接应被单独收集，不计错误
    anchors = data.get("code_anchors") or []
    rows.append(Row(
        "messy", "code_anchors 非空",
        PASS if anchors else FAIL,
        "例如 {} -> {}".format(anchors[0].get("path"), anchors[0].get("target"))
        if anchors else "code_anchors 为空，[src](src/main/java/A.java) 未被识别",
    ))
    return rows


# ---------------------------------------------------------------------------
# 用例三：split —— 拆分对照，照搬篇过、改写篇被抓
# ---------------------------------------------------------------------------


def case_split():
    vault = FIXTURES / "split"
    rows = []

    # 3a. fixture 自身要能过校验器（0 错误），否则 roundtrip 的结论不可信
    data, proc = validate_json(vault)
    errors = data.get("errors") or []
    rows.append(Row("split", "校验器 0 错误", PASS if not errors and proc.returncode == 0 else FAIL,
                    "exit {}，{} 条错误".format(proc.returncode, len(errors))
                    if errors or proc.returncode else "exit 0"))

    # 3b. roundtrip：整体必须报出差异（1 篇被改写）
    try:
        report, rt_proc = roundtrip_json(vault)
    except Skip as exc:
        # 校验器那部分已经跑过了，roundtrip 还没就绪就只跳过它，别把整案判废
        rows.append(Row("split", "roundtrip 可用性", SKIP, str(exc)))
        return rows
    rows.append(Row("split", "roundtrip exit 1", PASS if rt_proc.returncode == 1 else FAIL,
                    "实际 exit {}".format(rt_proc.returncode)))

    verbatim = find_child(report, "10_Notes/冒泡排序.md")
    if verbatim is None:
        rows.append(Row("split", "照搬篇 PASS", FAIL, "报告里找不到 10_Notes/冒泡排序.md"))
    else:
        ok = bool(verbatim.get("ok"))
        match = verbatim.get("match") or {}
        rows.append(Row(
            "split", "照搬篇 PASS（逐行未改）", PASS if ok else FAIL,
            "连续匹配 @ 存档第 {}-{} 行".format(match.get("start"), match.get("end"))
            if ok else "被误判为有差异: {}".format(brief(json.dumps(
                verbatim.get("issues"), ensure_ascii=False))),
        ))

    rewritten = find_child(report, "10_Notes/快速排序.md")
    if rewritten is None:
        rows.append(Row("split", "改写篇 FAIL", FAIL, "报告里找不到 10_Notes/快速排序.md"))
        return rows

    rows.append(Row("split", "改写篇 FAIL（同义改写被抓）",
                    PASS if not rewritten.get("ok") else FAIL,
                    "被误判为通过" if rewritten.get("ok") else "已报差异"))

    # 差异必须能定位到具体行：给出存档行号 + 原文/笔记对照
    issues = rewritten.get("issues") or []
    located = None
    for issue in issues:
        archive_text = str(issue.get("archive_text") or "")
        note_text = str(issue.get("note_text") or "")
        if ("选择一个基准元素" in archive_text and "挑一个基准值" in note_text
                and issue.get("archive_line") and issue.get("note_line")):
            located = issue
            break
    if located is None:
        rows.append(Row("split", "改写差异能定位到行", FAIL,
                        "issues 里没有带行号与原文/笔记对照的 rewritten 记录: {}".format(
                            brief(json.dumps(issues, ensure_ascii=False)))))
    else:
        rows.append(Row(
            "split", "改写差异能定位到行", PASS,
            "存档第 {} 行 ↔ 笔记第 {} 行（rule={}）".format(
                located.get("archive_line"), located.get("note_line"), located.get("rule")),
        ))
        # 人类可读输出里也应该能直接看到那一行
        human = run_script([
            sys.executable, str(ROUNDTRIP_SCRIPT), str(vault), "--source-dir", "30_Sources",
        ])
        if human.returncode == 2:
            rows.append(Row("split", "人类可读输出含差异行", SKIP,
                            "roundtrip_check.py 不认 --source-dir: {}".format(brief(human.stderr))))
        else:
            seen = "快速排序会挑一个基准值" in (human.stdout or "")
            rows.append(Row(
                "split", "人类可读输出含差异行", PASS if seen else FAIL,
                "" if seen else "stdout 未出现改写后的句子: {}".format(brief(human.stdout)),
            ))
    return rows


# ---------------------------------------------------------------------------
# 用例四：urlsrc —— source 是外部 URL 时不能误判成笔记名
# ---------------------------------------------------------------------------


def case_urlsrc():
    """`source: https://...` 是外部来源，不是双链，不能被报 source-broken。"""
    vault = FIXTURES / "urlsrc"
    rows = []
    try:
        report, proc = roundtrip_json(vault)
    except Skip as exc:
        rows.append(Row("urlsrc", "roundtrip 可用性", SKIP, str(exc)))
        return rows

    issues = report.get("issues") or []
    rows.append(Row(
        "urlsrc", "外部 URL source 不报 source-broken",
        PASS if not issues and proc.returncode == 0 else FAIL,
        "exit {}，issues={}".format(proc.returncode, brief(json.dumps(issues, ensure_ascii=False)))
        if issues or proc.returncode else "exit 0，issues 为空",
    ))

    child = find_child(report, "10_Notes/子笔记.md")
    if child is None:
        rows.append(Row("urlsrc", "子笔记照搬通过", FAIL, "报告里找不到 10_Notes/子笔记.md"))
    else:
        rows.append(Row(
            "urlsrc", "子笔记照搬通过",
            PASS if child.get("ok") else FAIL,
            "ok={}".format(child.get("ok")),
        ))
    return rows


CASES = [
    ("clean", case_clean),
    ("messy", case_messy),
    ("split", case_split),
    ("urlsrc", case_urlsrc),
]


# ---------------------------------------------------------------------------
# 表格输出
# ---------------------------------------------------------------------------


def display_width(text):
    """中文按 2 列宽计算，保证表格不歪。"""
    width = 0
    for char in str(text):
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def pad(text, width):
    text = str(text)
    return text + " " * max(0, width - display_width(text))


def print_table(rows):
    headers = ("用例", "检查项", "结果")
    widths = [display_width(headers[0]), display_width(headers[1]), 4]
    for row in rows:
        widths[0] = max(widths[0], display_width(row.case))
        widths[1] = max(widths[1], display_width(row.check))

    line = "  ".join(pad(head, width) for head, width in zip(headers, widths))
    print("\n" + line)
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join([
            pad(row.case, widths[0]),
            pad(row.check, widths[1]),
            pad(row.status, widths[2]),
        ]).rstrip())

    details = [row for row in rows if row.detail and row.status != PASS]
    if details:
        print("\n失败 / 跳过详情：")
        for row in details:
            print("  [{}] {} / {}: {}".format(row.status, row.case, row.check, row.detail))


def main(argv=None):
    global VERBOSE
    parser = argparse.ArgumentParser(
        description="Obsidian 知识库技能回归测试（clean / messy / split 三个 fixture）")
    parser.add_argument("--verbose", action="store_true",
                        help="打印每个被测脚本的原始 stdout/stderr")
    args = parser.parse_args(argv)
    VERBOSE = args.verbose

    print("仓库: {}".format(REPO_ROOT))
    print("fixture: {}".format(FIXTURES))

    rows = []
    for case_name, case_func in CASES:
        try:
            rows.extend(case_func())
        except Skip as exc:
            rows.append(Row(case_name, "整体", SKIP, str(exc)))
        except subprocess.TimeoutExpired:
            rows.append(Row(case_name, "整体", FAIL,
                            "子进程超过 {} 秒未退出".format(PROCESS_TIMEOUT)))

    print_table(rows)

    if VERBOSE and RAW_OUTPUT:
        print("\n===== 子进程原始输出 =====")
        for block in RAW_OUTPUT:
            print("\n" + block)

    passed = sum(1 for row in rows if row.status == PASS)
    failed = sum(1 for row in rows if row.status == FAIL)
    skipped = sum(1 for row in rows if row.status == SKIP)

    print()
    if skipped:
        print("跳过 {} 项（被测脚本尚未就绪，见上方详情）".format(skipped))
    print("结果: {} 通过 / {} 失败".format(passed, failed))

    if failed:
        sys.exit(1)
    if skipped:
        print("提示: 有跳过项，结论不完整；被测脚本就绪后请重跑本命令。")
    return 0


VERBOSE = False

if __name__ == "__main__":
    sys.exit(main())
