---
name: obsidian-vault-builder
description: >
  Use when a markdown notes folder or Obsidian vault has become unusable: notes
  flat at the root, one 3000-line file that should be thirty notes, no
  frontmatter, no backlinks, broken relative links, dead MOCs — and the user
  asks to 整理 / 重构 / 规范化 / 拆分 / 建知识库 / 归档. Also use for the
  incremental case in an already-built vault: 记一条 / 收进库 / 收藏一篇 /
  新增笔记 / 收件箱（00_Inbox）里堆了新东西 / 把这篇/这批新文件放进知识库 —
  decide 新增模式 vs 改造模式 per file, place it, wire it into the vault,
  then validate the whole vault. Also use for a messy
  code or docs repository whose markdown should become an orderly, linked vault
  (仓库整理 / 文档站保持导航不动), when they ask how to connect Obsidian to an AI
  (MCP vs direct file access), or how to keep an AI from rewriting their notes.
license: MIT
---

# Obsidian 知识库构建与改造

把「一堆散落的 md 文件」变成「可检索、可链接、可演化的知识库」，并且保证原文一个字都不丢。

## 核心原则

1. **vault 就是文件夹。** AI 直接读写 markdown 文件就够了，MCP 不是必需品。
2. **正文不许重写。** 改造 = 搬家 + 补元数据 + 建链接。原文全量留档，改完用 git 的 rename 相似度自证正文未变（>90% 即通过）。增量任务里，库外新输入允许结构化整理，但**已成库的笔记正文在任何模式下都不动**。
3. **链接不许伪造。** 坏的链接改成纯文本并注明去向，不要编一个看起来能点开的目标。
4. **每一步都能回滚。** 暂存区建好 → 校验通过 → 一次性安装 → commit。不在原地边改边想。

## 何时不用这个技能

需要 Obsidian **运行时能力**（Dataview 查询、Bases 视图、反链图遍历、模板变量渲染）才需要 MCP：

- 通用方案：装社区插件 **Local REST API**（默认 27123 明文 / 27124 HTTPS），再用外部 MCP server 连它（如 `mcp-obsidian`、`obsidian-mcp-server`）。
- 省事方案：插件 **`semantic-vault-mcp`**（仓库 `aaronsb/obsidian-mcp-plugin`）本身就是 MCP server，不需要外部 Node 进程。

纯读写的重构任务不碰 MCP——多一层协议只会多一层失败面。

## 三条分支

| 用户要什么 | 走哪条 |
|---|---|
| 结构乱、要重构 | 下面的五步改造流程 |
| 记一条新东西 / 库已成型又进来新文件 | 下面的「增量流程」：取变更集 → 判模式 → 查重落库挂链 → 全库校验 |
| 只是问问题 | 只读，不写任何文件 |

**先判仓库还是笔记库，再决定要不要搬。** `inventory.py` 报 `code-repo` / `docs-site` / `mixed` 时，先看这两条：

- **`docs-site`**（有 `mkdocs.yml` / `_sidebar.md` / VuePress、Docusaurus 配置）：目录本身就是导航，路径被配置硬编码。**不要套 `00_`/`10_` 骨架，不要搬文件改名**，否则站点直接 404。只做三件事：给笔记类 md 补 frontmatter、修真实死链、清掉明显的空文件与重复页。**页面之间不需要互链**，所以校验要加 `--profile docs-site`（跳过 `no-outbound` / `one-outbound` / `orphan` / `not-indexed-by-moc` / `root-note` 这些笔记语义检查；死链、frontmatter、重名照旧检查）。用户坚持重组时，先改导航配置再搬文件，并把被导航引用的路径逐条列进映射表。
- **`code-repo`**：代码、构建产物、依赖目录（`node_modules`、`dist`、`.venv`）一律不碰；只对 `docs/`、`*.md` 说明文件按笔记规则处理。

`move_blocked` 里列出的路径，就是「搬了会坏」的清单。

## 改造流程

### 0. 基线快照（先做，做不了就别开工）

改造前的库必须留下一份可以逐字节比对的底本：

```bash
git init && git add -A && git commit -m "baseline: 改造前"
git tag pre-vault-refactor        # 已有 git 仓库时：确认 git status 干净，再打标签
```

提交前先确认本机配了 git 身份；报 `Author identity unknown` 就在**该仓库内**补一次
`git config user.name "vault-refactor" && git config user.email "vault-refactor@local"`（不碰全局配置）。

目标库不方便进 git 时，把整库复制到会话工作区 `_baseline/` 存着。**没有基线快照，「没丢内容」「正文没改」两句话都无法自证**，直接停机问用户，不要靠「我很小心」开工。

### 1. 盘点（不许跳过）

先跑盘点工具，别靠肉眼和 `ls`：

```bash
python scripts/inventory.py <vault_root>          # 人类可读
python scripts/inventory.py <vault_root> --json   # 机器可读，便于自己二次筛
```

它给出：文件与扩展名分布、每篇 md 的行数/出链/嵌入/BOM/CRLF、切分候选榜（按 `^## N.` 计数）、重名与疑似重复、失效链接与代码仓库锚点、附件孤儿、工程类型（`notes-vault` / `code-repo` / `docs-site` / `mixed`）、以及被文档站导航引用的路径。人工补充：

- **现有的 `AGENTS.md` / `README.md` 要读，但不要盲从**——它很可能就是这次问题的来源（比如写着「所有笔记放根目录」）
- 统计长篇文件里的切分单位：`## N.` 这类同级标题有几个，就是几篇
- 已经存在的双链与相对链接先扫一遍，记下哪些是失效的

### 2. 设计映射（先给用户看，再动手）

给出一张「旧路径 → 新路径」的对应表。目录骨架默认：

```
00_Inbox/   未加工的输入        10_Notes/   一个概念一篇
20_MOC/     主题入口与导航       30_Sources/ 原文存档
40_Projects/ 有产出的项目       90_Journal/ 日志（无内容不建）
99_Archive/ 归档（淘汰不删除）   _templates/ 模板
_attachments/ 附件（图片/PDF/表格）
```

不建空目录。`90_Journal/`、`99_Archive/` 没有内容就不创建；`_attachments/` 只在真有附件要搬时创建。

### 3. 机械拆分（改造的主战场）

**顺序不能反：先把原文整份复制到 `30_Sources/` 存档，再从副本切分。** 反过来做就是不可逆操作。

按标题机械切分，**不做内容改写**：

- 切分单位 = 与原文档同级的 `## N.` 章节；一个章节一篇笔记
- 文件名：去掉编号和空格，能独立表意（`02 对话持久化.md` → `对话持久化与断线续传.md`）
- **标题降级**：原 `###` → 新 `##`，原 `####` → 新 `###`。不降级会出现「`#` 下面直接 `###`」的跳级
- **只降级，不合并，不加粗，不加引用号。** 原文的 `### 简历描述` 必须原样成为新笔记的 `## 简历描述`，不能压成 `> 简历描述：…`；原文 `考察点：` 不能改成 `**考察点**：`。判定标准：把新笔记的标题去掉、把上层标题重新插回去，应该能和原文逐行对上
- 原文存档那一篇，每篇新笔记的 `source` 用双链指回它（`source: "[[面试题库原文]]"`），而不是写原路径字符串
- 拆完的每一篇都要能在原文里指回自己的位置（存档笔记的目录/标题就是索引）
- 不拆的情况：不足 150 行；结构不是按标题组织的（长表格、时间线、代码清单）
- **附件（图片/PDF/表格/音视频）原样复制到 `_attachments/`**，正文里的 `![[图.png]]` 保留原文件名。Obsidian 按文件名全库解析嵌入，所以搬目录不用改引用；改名会产生 `broken-embed`，等于制造死链

### 4. 连接

- 每篇内容笔记的**正文**里要有 ≥ 2 条双链：一个 `## 相关` 小节（每条写清为什么相关）+ 至少一条指向所属 MOC。
- **`source:` 字段里的双链不算。** 只靠 MOC 链进来也不算——笔记要自己长出链接。
- 每篇笔记必须至少被一个 MOC 索引到；孤儿笔记 = 交付不合格
- 修坏链接时三选一：找到真实目标就改成双链；目标在代码仓库就保留原样；都不行就改成纯文本并注明「原仓库文件，本库未收录」

### 5. 校验 → 安装 → 提交

```bash
python scripts/validate_vault.py <vault_root>                        # 笔记库
python scripts/validate_vault.py <vault_root> --profile docs-site    # 文档站
```

必须 **0 错误**才能进下一步。文档站不要为了消 `no-outbound` 硬塞 `## 相关` 链接——那不是文档站的形状，用 `--profile docs-site` 才是对的。拆分过的库再跑一次往返检查，逐篇证明正文没被动过：

```bash
python scripts/roundtrip_check.py <vault_root>                  # 默认：未覆盖记 WARN
python scripts/roundtrip_check.py <vault_root> --require-full   # 严格：有子笔记就必须逐行覆盖
```

它要求每篇拆出来的笔记，正文必须是 `30_Sources/` 存档正文的**连续子序列**（允许整段少，不允许改一个字），标题只允许降级一级。退出码非 0 就是有人「顺手改好了」。这比 `git rename` 相似度强：相似度只能证明原件进了存档，证明不了拆出来的每一篇没被改写。

然后：

1. 暂存区（会话工作区内）建好全部产物
2. 逐文件比对字节数，一致才算复制成功
3. 用户确认后再删除旧目录
4. `git add -A && git commit`
5. 自证：`git show --stat` 里原文件应显示为 `rename ... (95%)`，相似度过低说明正文被改了，回滚

## 增量流程（库已成型，又有新文件进来）

库已经整理好了，用户又丢进来一篇或一批新东西：「记一条」「收进库」「加一篇笔记」「Inbox 里堆了一堆」。**这不是重跑改造流程**——跳过基线快照和全库映射表，只处理增量；但入口和出口一步都不能省。

### 1. 先取变更集，不要靠猜「哪些是新的」

```bash
git tag vault-incremental-2026-11-03          # 处理前打点，这是下一轮的基线
python scripts/inventory.py <vault_root> --since <上一轮的 tag 或 commit>
```

`--since` 用 `git diff --name-status -M` 加未跟踪文件，给出 added / modified / deleted / renamed / untracked 五类，并把 md 子集单独列出来（`md_added` / `md_modified` / `md_deleted` / `md_renamed`）。已提交的旧笔记不会出现在结果里——这就是「哪些是新的」的答案，比看 mtime 靠谱（复制、同步盘、`git checkout` 都会改 mtime）。

- 目标库不是 git 仓库时 `--since` 退出码 2：改成让用户给显式清单（`00_Inbox/` 里的文件、或「就这一篇」），并在汇报里注明「本次变更集来自用户清单」。
- 没有上一轮基线（第一次增量）：用「`00_Inbox/` 里没处理过的文件 + 用户指名的新文件」当变更集，处理完把当前 HEAD 打上 tag 作为下次基线。

### 2. 逐篇判模式，判不准就问

| 这一篇从哪来 | 走哪个模式 | 允许对正文做什么 |
|---|---|---|
| 库外的新输入（用户写的、从网页或别处拖进来的） | **新增模式** | 允许结构化整理：补 frontmatter、顺标题层级、补 `## 相关`；**不许编造原文没有的事实与数字** |
| 库里已有文件的副本 / 旧版本 / 改过一遍的稿子 | **改造模式** | 一字不改：整份进 `30_Sources/` 存档，再按改造流程第 3 步机械拆分，跑 `roundtrip_check.py` |
| 判不准 | —— | 停下来问一句：「这篇是外部新内容，还是已有笔记的新版本？」 |

区分标准是**来源**，不是「正文看起来像不像被改过」。同一批里两种都有时逐篇分，不要整批套一个模式。

### 3. 落库：查重 → 落文件 → 挂链

1. **查重（先做，别先写）**：标题和首段拿去全库搜（`grep`，或看 `inventory.py` 的重名与疑似重复）。会撞车就并进原笔记补一节，别新建一篇 90% 重复的；并在笔记里注明合并来源。
2. **落文件**：放最贴近的目录（`10_Notes/` 一个概念一篇、`40_Projects/` 有产出的、`90_Journal/` 按天的）。frontmatter 八个字段一个不少：`created` 用原文的创作时间（不知道就写处理当天并在正文注明），`updated` 写今天，`status: seed`。
3. **挂链**：`## 相关` 里 ≥ 2 条正文双链并写清为什么相关；再链进至少一个 MOC（MOC 里加一行索引 + 一句为什么归这个主题）。
4. **旧文件只动两处**：给老笔记加链接行、把 MOC 的索引行补上。这是增量里唯一允许碰存量文件的地方——不改老笔记正文、不改名、不重排标题。

### 4. 校验要跑全库，不是只校验新文件

```bash
python scripts/validate_vault.py <vault_root>        # 传全库，不要只传新文件
```

新笔记的链接可能指向旧笔记的旧名；`dup-stem`、`broken-wikilink`、`orphan`、`not-indexed-by-moc` 只有全库视角才看得见。0 错误再提交，commit message 写清「incremental: 新增 N 篇」。走改造模式的那几篇还要加跑 `roundtrip_check.py`。

### 5. `00_Inbox/` 收尾

- 处理完的输入移进 `99_Archive/`（**原名、原 frontmatter 都不改**）；如果 Inbox 里那一篇本身就是最终成品（不是副本），直接移到目标目录，别留两份。告诉用户选了哪个。
- **留在 `00_Inbox/` 的 md 永远不会报警**：校验器对 Inbox 豁免出链与 MOC 检查。所以 Inbox 里出现的每一篇都必须在这一轮被处理掉，不能「先放着」。
- 处理完再打一个本轮 tag，下次 `--since` 用它当基线。

### 增量流程不做的事

- 不重跑盘点的全量结论，不重做旧→新映射表（上一轮已经做完了）
- 不因为「反正要动」就顺手给旧笔记改名、拆分、重排
- 不建空 MOC：新笔记够不上一个主题，就挂在最接近的上级 MOC 下
- 不在 `00_Inbox/` 里写 `[[ ]]` 占位再指望以后补

## 硬性边界

1. **不丢内容。** 原件被搬走或拆分之前，必须先有一份逐字节相同的副本（`30_Sources/` 存档，或 `99_Archive/` 归档并保留原 frontmatter）。副本校验通过后，被它取代的原件才允许删除；**没有副本就一个字都不许删**。
2. **改造模式不重写正文。** 存量笔记的正文在任何模式下都不动；要压缩、要重写，先问用户。改造任务里任何「顺手改好了」都是越界。（增量任务里，库外新输入允许补字段、补链接、顺标题层级——这是结构化整理，不是重写。）
3. **不伪造链接。** 见核心原则 3。
4. **不改写源码锚点。** `File.java:12-34`、`../src/main/java/...`、`路径:行号` 指向的是代码仓库，不是本库文件。它们「点不开」是正常的，**不要改成双链，不要删除**。校验脚本会单独列出它们。
5. **不臆造事实与数字。** 信息不足写 `> 待确认：...`。尤其不要编指标（MRR / NDCG / QPS / 延迟）。
6. **不建空目录、空 MOC、占位文件。**
7. **frontmatter 八个字段固定**，不自创字段：`title type status tags created updated source confidence`。
8. **每篇笔记 ≥ 2 条双链**，孤儿笔记不允许存在。

## 你一定会想到的借口（无技能实测全部踩过）

左边这些话听起来都非常合理，全部是错的：

| 你的想法 | 为什么不能 | 怎么办 |
|---|---|---|
| 「拆完删掉原文件，内容只留一份，避免两处不同步」 | 原文是唯一能自证的底本。删了之后，笔记里的措辞有没有被动过，永远说不清。「不同步」由 git 解决，不由删除解决 | 原文整份进 `30_Sources/`，正文不动，`source` 双链指回 |
| 「`### 简历描述` 只是个标签，压成引用更紧凑」 | 那是原文结构的一部分。改了就做不到逐行对照 | 只降级不合并：`###`→`##`，`####`→`###` |
| 「`aliases` / `project` 字段很有用，顺手加上」 | 字段名和枚举一旦被改，按类型查询、模板、校验脚本全部失配 | 只写 8 个固定字段；要加先问用户 |
| 「`type: 面试题` 比 `note` 更语义化」 | 枚举必须闭合。语义应该进 `tags`，不是进 `type` | `note`/`moc`/`source`/`project`/`journal` 五选一 |
| 「这个死链得消掉，不然校验不过」 | 消死链是最容易滑向编造的地方——会凭空长出一篇「架构说明」 | 目标不存在 → 改纯文本 + 登记缺口。**宁可校验报错，也不造笔记** |
| 「文件名带空格更好读」 | 空格在命令行、脚本、URL 里都要转义 | 去空格，中文直接连写 |
| 「结构差不多就行，先装上再说」 | 装完再改，就是在真库里原地施工，没有回滚点 | 暂存区 + 校验 0 错误 + 一次安装 |
| 「增量就把全量改造再跑一遍最稳」 | 已经整理好的结构会被再动一次，风险远大于收益，也没有回滚点可言 | 用 `--since` 只处理变更集；出口仍然是对全库跑一次校验 |
| 「这篇不确定放哪，先丢 `00_Inbox/`」 | 校验器对 Inbox 豁免出链与 MOC 检查，留在那里永远不会报警，等于隐身 | 这一轮就判模式、落位；判不了就问用户 |

## 校验契约

`scripts/validate_vault.py` 的输出里：

- `ERROR` 必须为 0，否则修到 0 再装
- `WARN` 里的孤儿笔记要逐条处理（真孤儿挂 MOC，模板与索引文件豁免）
- 「正文零出链」是 ERROR：拆出来的笔记必须自己写 `## 相关`，不要指望 MOC 兜底
- 「代码仓库锚点」那一栏是**正常情况**，只是告诉你这里有多少个指向库外的锚点，不要动它们
- `![[图.png]]` 这类**嵌入**走附件解析：文件存在就算通过，不会要求你把它改成笔记双链；不存在才报 `broken-embed`
- 全路径双链（`[[notes/a/Guide]]`）与带 `<尖括号>`/`%20` 的 markdown 链接都按真实路径解析
- `--json` 输出可被程序解析（`errors` / `warnings` / `code_anchors` / `exit_code`），`--max-errors N` 调文本模式的上限（JSON 永不截断）
- `fence-unclosed` 说明有围栏没闭合，其后内容被当成代码、链接检查已跳过——先补围栏再看链接结论
- **注释、行内代码、缩进代码块里的 `[[X]]` 不是链接**（`%%...%%`、`<!-- -->`、行内 `` `...` ``、空行后 4 空格缩进）。这些是写给人的，不要为了清零错误去改，校验器也不会报
- **逃出 vault 的相对链接**：指向源码树（`../src/...`、`Foo.java:12`）记为「代码仓库锚点」，不算错误；目标存在但不是代码 → `WARN outside-link`；目标不存在 → `ERROR broken-relative-link`
- `roundtrip_check.py --require-full`：只要存档拆出了子笔记，就必须逐行覆盖，有未覆盖行直接 exit 1。默认模式不因未覆盖失败（存档保留全书，覆盖不全可能是正常取舍）
- **一篇笔记正文最多一个 H1**。拆分时原文的第一个 H1 归第一篇；后续篇章从 `##` 起，不要为了让每篇「看起来完整」各补一个 H1。原文 frontmatter 之前的说明段同样只归第一篇
- **重名与大小写**：全库 `basename` 重名报 `dup-stem`（`[[X]]` 会歧义）；双链解析**大小写敏感**，按 Linux 语义判，`[[guide]]` 不会匹配 `Guide.md`
- `status` 只取 `seed` / `growing` / `evergreen`，`confidence` 只取 `high` / `medium` / `low`；`evergreen` 必须有入链，否则 WARN
- 退出码：`0` 无错误（警告允许）· `1` 有错误 · `2` 用法错误

## 平台坑（都是踩过的）

- **沙箱**：目标目录在会话工作区外时写入会被拒。做法是全部产物先在会话工作区建好，安装时用一次提权操作完成，不要逐个文件提权。
- **PowerShell 变量大小写不敏感**：`$out = @()` 和 `$Out = '输出目录'` 是同一个变量，会互相覆盖导致 `Join-Path` 收到空字符串。集合和路径不要同名。
- **`| Select-Object -First N` 会掐断上游脚本**：表现为「只生成了 2 篇就走完了」。不要在调用生成脚本时接这个管道。
- **写文件用无 BOM UTF-8**：`[System.IO.File]::WriteAllText($p, $t, (New-Object System.Text.UTF8Encoding($false)))`。
- **ABSOLUTELY 不要在当前工作目录放 `AGENTS.md` 模板**：任何目录下的 `AGENTS.md` 都会被自动加载成指令。技能里的模板文件要改名（本技能用 `templates/vault-AGENTS.md`），落地时才复制成目标库根目录的 `AGENTS.md`。

## Red Flags — 出现这些就停下来

- 你打算「顺便优化一下文字」
- 你打算拆完之后把原文删掉
- 你在给 frontmatter 加第 9 个字段
- 你在删一个「点不开」的链接
- 你说「结构差不多就行，先提交」
- 你在原地直接改，没有暂存副本
- 校验脚本还没跑你就说已完成
- 你在增量任务里给旧笔记改名 / 重排标题 / 顺手润色正文
- 你把没处理的文件留在 `00_Inbox/` 就说做完了（那里永不报警）

**以上任一条成立，说明你已经偏离了「搬家 + 标注 + 连接」的边界。**
