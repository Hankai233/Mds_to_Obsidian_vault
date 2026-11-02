# obsidian-vault-builder

**把一堆散落的 Markdown 笔记，整理成可检索、可链接、可长期演化的知识库——并且保证原文一个字都不丢。**

这是给人看的说明文档。AI agent 实际执行时读的是 [SKILL.md](SKILL.md)，`README.md` 不会被加载。

## 它解决什么问题

「帮我整理下我的 Obsidian 库」这句话，在没有任何约束的情况下会让模型做出下面这些事（都是实测记录，不是假设）：

| 失败 | 具体表现 | 后果 |
|---|---|---|
| 拆完把原文删掉 | 314 行的 `面试题.md` 拆成 5 篇后原文件消失，理由是「避免两处不同步」 | 不可逆。笔记里的措辞有没有被动过，再也无法自证 |
| 顺手改写正文 | `### 简历描述` 被压成 `> 简历描述：…`，`#### Q1:` 被提成 `## Q1：`，`考察点：` 被加粗 | 读起来毫无破绽，但失去取证价值 |
| 自创 frontmatter | `type: 面试题`、`status: 已整理`、多出 `aliases`/`project`、少 `confidence` | 按类型查询、模板、脚本全部失配 |
| 为了消死链去造文档 | 失效链接 `docs/architecture.md` → 凭空生成一篇 1830 字节的「架构说明」 | 库开始混杂编造内容 |
| 文件名带空格 | `WebSocket 流式对话与主动中断.md` | 命令行、脚本、URL 全要转义 |

这个技能把边界写死，并提供校验器把这些错误变成可执行的失败（非零退出码）。

## 适合谁 / 不适合谁

**适合**（任意一种成立就值得用）：

- 笔记平铺在根目录，或目录分类已经乱掉
- 有一篇几千行的长文，本该是几十篇笔记
- 没有 frontmatter，或字段每个人写得都不一样
- 笔记之间几乎没有 `[[双链]]`，MOC 是空的或早已失效
- 搬家/改名之后留下大量相对链接死链

**不适合**：你需要的是 Obsidian **运行时能力**——Dataview 查询、Bases 视图、反链图遍历、模板变量渲染。那才需要 MCP：

- 通用方案：装社区插件 **Local REST API**（默认 27123 明文 / 27124 HTTPS），再用外部 MCP server 连它（如 `mcp-obsidian`、`obsidian-mcp-server`）。
- 省事方案：插件 **`semantic-vault-mcp`**（仓库 `aaronsb/obsidian-mcp-plugin`）本身就是 MCP server，不需要外部 Node 进程。

纯读写的重构任务不碰 MCP——多一层协议只会多一层失败面。**vault 就是一个文件夹，直接读写 `.md` 就够了。**

## 四条硬承诺

1. **不丢内容。** 原件被搬走或拆分之前，必须先有一份逐字节相同的副本（`30_Sources/` 存档，或 `99_Archive/` 归档）。没有副本，一个字都不许删。
2. **不重写正文。** 改造 = 搬家 + 补元数据 + 建链接。拆出的笔记只做标题降级（`###`→`##`、`####`→`###`），去掉标题后能反向还原成原文；git 里原文件应显示为 `rename` 且相似度 >90%。要压缩、要重写，先问你。
3. **不伪造链接。** 坏链接三选一：找到真实目标改成双链 → 目标在代码仓库就保留原样 → 都不行改成纯文本并注明「原仓库文件，本库未收录」。**宁可校验报错，也不造笔记。**
4. **每一步都能回滚。** 暂存区建好 → 校验 0 错误 → 一次性安装 → `git commit`。不在真库里原地边改边想。

## 快速开始

### 1. 安装

把整个目录复制到 agent 的技能目录（以 DSH 为例，目录名必须是 `obsidian-vault-builder`，与 SKILL.md 里的 `name` 一致；仓库名 `Mds_to_Obsidian_vault` 与技能标识无关）：

```
C:\Users\<you>\.dsh\skills\obsidian-vault-builder\
```

PowerShell：

```powershell
$dst = "$env:USERPROFILE\.dsh\skills\obsidian-vault-builder"
New-Item -ItemType Directory -Force $dst | Out-Null
Copy-Item -Recurse -Force .\* $dst
```

如果目标目录在会话沙箱之外，写入需要一次提权操作。装完用 SHA256 比对源目录和安装目录，确认文件全等，然后开一个新会话，技能列表里应出现 `obsidian-vault-builder`。

### 2. 触发

直接说人话即可，`description` 里的触发词覆盖「整理 / 重构 / 规范化 / 拆分 / 建知识库 / 归档」，也覆盖混着代码与文档的仓库：

- 「把这个目录整理成 Obsidian 知识库：`E:\notes`」
- 「`面试题.md` 有 3000 行，拆成一个技术点一篇」
- 「库里的 frontmatter 和双链不规范，帮我规范化一下」

### 3. 过程

agent 会分七步走。**第 0 步先打基线快照**（git commit + tag，或整库复制到 `_baseline/`）；**第 2 步会先给出「旧路径 → 新路径」映射表等你确认**；旧文件只有在副本校验通过、并且你点头之后才会被删除：

```
基线快照 → 盘点 → 设计映射 → 机械拆分 → 建链接 → 校验 → 安装 → 提交
```

### 4. 校验与验收

```bash
python scripts/inventory.py <vault_root>       # 动手前：先看清楚烂在哪
python scripts/validate_vault.py <vault_root>  # 安装前：必须 0 错误
python scripts/roundtrip_check.py <vault_root> # 拆分过的库：逐篇证明正文没被改
```

安装后自查：

```bash
git show --stat        # 原文件应显示为 rename，相似度 >90%；过低说明正文被改了
git revert <commit>    # 不满意就整体回滚
```

想先确认工具链本身没坏，跑 `python tests/run_eval.py`（18 项，含四个夹具库）。

## 仓库结构

```
obsidian-vault-builder/
├── SKILL.md                      # 技能本体，agent 读这个
├── README.md                     # 本文件，给人看
├── scripts/
│   ├── inventory.py              # 只读盘点：分布、切分候选、重名、死链、工程类型
│   ├── validate_vault.py         # 零依赖校验器（Python 3 标准库）
│   └── roundtrip_check.py        # 往返检查：拆出的笔记正文是否被动过
├── templates/
│   ├── 原子笔记.md                # 一个概念一篇
│   ├── 问答型笔记.md              # 面试题 / 故障复盘（已含标题降级后的层级）
│   ├── 来源笔记.md                # 原文存档
│   ├── MOC.md                     # 地图笔记
│   └── vault-AGENTS.md           # 库内规范骨架，落地时复制成目标库根目录的 AGENTS.md
└── tests/
    ├── run_eval.py               # 一条命令跑完全部回归
    └── fixtures/{clean,messy,split}/   # 三个夹具库
```

三个脚本都是 Python 3 标准库、零第三方依赖，可脱离技能单独用。

`templates/vault-AGENTS.md` **故意不叫 `AGENTS.md`**：任何目录下的 `AGENTS.md` 都会被 harness 自动加载成指令，放在技能目录里会污染当前会话（实测已触发过一次）。

不捆绑「一键拆分脚本」：每篇长文的标题结构都不同，泛化脚本比手工拆分更脆。拆分规则写在 [SKILL.md](SKILL.md) 第 3 步。

## 三个脚本

### 盘点：`inventory.py`

改造前先跑它，别靠肉眼和 `ls`：

```bash
python scripts/inventory.py <vault_root>            # 人类可读
python scripts/inventory.py <vault_root> --json     # 机器可读
python scripts/inventory.py <vault_root> --top 20 --max-files 500
```

输出：文件与扩展名分布、每篇 md 的行数/出链/嵌入/BOM/CRLF、切分候选榜（按 `^## N.` 计数）、重名与疑似重复、失效链接与代码仓库锚点、附件孤儿、工程类型（`notes-vault` / `code-repo` / `docs-site` / `mixed`）、被文档站导航引用的路径（`move_blocked`）。**只读，不改任何文件。**

几个和校验器保持一致的语义，别把两份报告读岔：

- **围栏内的链接不算死链**（与校验器同一套状态机）；围栏没闭合时单独提示「围栏未闭合，其后链接未检查」，不把后面的正文当死链。
- **`doc_refs` 分三种状态**：没有导航配置 / 导航引用了 N 个路径且都存在（才给 `move_blocked`）/ 配置与文件已脱节（`existing: 0`，先核对 `docs_dir` 与 `nav`，此时不要据此移动文件）。
- `--json` 顶层含 `files dirs md_files dead_links duplicates attachments doc_refs hygiene big_files build_systems code_anchors fence_unclosed project_kind recommendations` 等字段，可程序化消费。

### 校验：`validate_vault.py`

```bash
python scripts/validate_vault.py <vault_root>                 # 完整输出
python scripts/validate_vault.py <vault_root> --quiet         # 只输出结论
python scripts/validate_vault.py <vault_root> --json          # 程序可读
python scripts/validate_vault.py <vault_root> --max-errors 50 # 文本模式上限（默认 200）
```

退出码：`0` 无错误（警告允许）· `1` 有错误 · `2` 用法错误。可直接接进 CI。

`--json` 顶层：`{"vault","md_files","errors":[{"path","rule","message"}],"warnings":[...],"code_anchors":[{"path","target"}],"exit_code"}`，永不截断。

**ERROR（15 条）**

| rule | 含义 |
|---|---|
| `frontmatter-missing` | 缺 frontmatter（根 `README.md`、`AGENTS.md` 豁免） |
| `field-missing` / `field-unknown` | 缺 8 个必需字段之一 / 多出第 9 个字段 |
| `type-invalid` `status-invalid` `confidence-invalid` | 取值不在枚举内 |
| `date-invalid` | `created`/`updated` 不是真实存在的 `YYYY-MM-DD` |
| `type-dir-mismatch` | `type` 与目录前缀矛盾（`10_Notes`→note、`20_MOC`→moc、`30_Sources`→source、`40_Projects`→project、`90_Journal`→journal） |
| `broken-wikilink` / `broken-embed` | 双链 / 嵌入目标不存在（两者分流：附件按文件名解析） |
| `broken-relative-link` | markdown 相对链接不存在（代码仓库锚点除外） |
| `heading-jump` | 标题跳级（`#` 下直接 `###`） |
| `no-outbound` | `type: note/project` 正文零双链（`00_Inbox`/`99_Archive` 豁免） |
| `dup-stem` | 两篇同名 `.md`，双链会指向歧义 |
| `source-broken` | `type: source` 的 `source` 字段没有指回它的存档关系 |

**WARN（11 条）**

`orphan`（零入链）· `one-outbound`（只有 1 条出链）· `title-mismatch`（title 与文件名或首个 H1 不一致）· `filename-space` · `evergreen-no-inbound` · `root-note`（笔记放在库根）· `multi-h1` / `no-h1` · `fence-unclosed`（围栏未闭合）· `not-indexed-by-moc`（没有 MOC 索引到它，`00_Inbox`/`99_Archive` 豁免）

### 往返检查：`roundtrip_check.py`

```bash
python scripts/roundtrip_check.py <vault_root>
python scripts/roundtrip_check.py <vault_root> --json
python scripts/roundtrip_check.py <vault_root> --allow-added "相关,延伸问题"
```

拆出来的每篇笔记，正文必须是 `30_Sources/` 存档正文的**连续子序列**：允许整段少于原文，不允许改一个字；标题只允许降级一级（`###`→`##`）。退出码 `0` 全过 · `1` 有改写/失配 · `2` 用法错误。

- 为什么是连续子序列而不是「这些行都在原文里」：行集合对得上，段落照样可能被挪位、被吞重复行、被跨章拼接。
- 空正文的子笔记判 **FAIL**（拆分整段丢失的现场就是空壳笔记）。
- `--allow-added` 是**默认值 + 追加**（默认 `相关,延伸问题`，传空串才清空）；允许新增的小节整段不参与比对。
- `--json` 顶层：`{"vault","sources":[{"path","core_lines","children":[...],"coverage"}],"issues","unlinked_notes","exit_code"}`，每个 child 带 `ok`、`match{start,end}`、`issues[]`（含 `archive_line`/`note_line`/两侧原文）。
- 覆盖率只累加「连续匹配成功」的行——顺序全乱但每行都在的笔记会显示 0%，这是故意让问题显眼。

它比 `git rename` 相似度更硬：相似度只能证明原件进了存档，证明不了拆出来的每一篇有没有被顺手改写——后者才是实测里真正发生过的越界。

## 校验器的判定细节

1. **围栏代码块先被剥离再扫链接**，否则文档里举例的 `[[相关笔记 A]]` 会被当成失效链接。围栏没闭合时，报 `fence-unclosed`，其后内容整体按代码处理（链接检查跳过）。
2. **行内代码不会被剥离。** 写成 `` `[[X]]` `` 的示例仍会被当作真实链接；写链接示例请用围栏代码块。
3. **嵌入与双链分流**：`![[图.png]]` 按附件扩展名集合解析（png/jpg/gif/svg/webp/pdf/docx/xlsx/pptx/mp3/mp4/zip/canvas/excalidraw 等），存在即通过；`![[某笔记]]` 或普通 `[[笔记]]` 按笔记解析。全路径双链 `[[notes/a/Guide]]`、带 `<尖括号>` 与 `%20` 的 markdown 链接都按真实路径解析。
4. **指向代码仓库的锚点不算死链**，单独汇总成「代码仓库锚点」——不要修它们。
5. **`00_Inbox/` 与 `99_Archive/` 豁免出链检查与 MOC 索引检查**（未加工输入、归档件不需要自己长链接）。
6. **大小写敏感**：`[[Guide]]` 不会匹配 `guide.md`。Windows/macOS 上 Obsidian 不区分大小写，Linux 上区分——校验器选严格的一侧，宁可报错也不放过跨平台会坏的链接。

## 实测效果

早期版本做过一次「无技能 / 有技能」对照，同一份 fixture 两种条件各跑一次。这张表现在被固化成可复跑的回归夹具，不再靠回忆：

```bash
python tests/run_eval.py           # 可加 --verbose 看子进程原始输出
```

`tests/fixtures/` 四套夹具：`clean`（必须 0 错误 0 警告）、`messy`（覆盖全部 15 条 ERROR 与 9 条 WARN，且断言 `![[存在的图.png]]` 与 `[[notes/a/Guide]]` 不误报）、`split`（一篇照搬、一篇被同义改写，往返检查必须抓出后者）、`urlsrc`（`source: https://...` 是外部来源不是双链，往返检查不得误报 `source-broken`）。

| 维度 | 无技能 | 有技能 |
|---|---|---|
| 校验结果 | 49 错误 / 8 警告 | **0 错误 / 0 警告** |
| 原长文 | 删除，只剩拆出的 5 篇 | `30_Sources/` 存档，正文字符数与原文逐一相等 |
| 正文完整性 | 结构折叠 + 加粗改写 | 反向还原后与原文逐行相等 |
| frontmatter | 自创 9 字段 | 固定 8 字段，`source` 双链指回存档 |
| 失效链接 | 凭空生成「架构说明」 | 改纯文本 + 注明 + MOC 登记缺口 |
| 旧 AGENTS.md | 直接覆盖 | 归档保留 |
| 标题层级 | `#` 下跳 `###` | `#` → `##` → `###` 逐级下降 |

第一轮带技能的运行仍然出现了「拆出的笔记正文零出链」——因为最初的校验器只查入链不查出链。补上规则、把「自己写 `## 相关`，别指望 MOC 兜底」写进流程后，第二轮才真正 0 错误。**这类漏洞不跑测试是发现不了的**——所以现在有了 `tests/`。

## 已知限制

- **增量分支没有对照测试。** 「收件箱单条输入 → 落一篇笔记」的流程和边界都写了，但只验证过存量库重构。
- **仓库模式只有清单、没有端到端实战。** `inventory.py` 能识别 `docs-site`/`code-repo` 并列出被导航引用的路径（`move_blocked`），SKILL.md 也写了「不搬目录」的分支；但「把真实 mkdocs 仓库改造成 vault」还没作为夹具跑过一遍。
- **往返检查只覆盖「存档 → 拆分」这条主线。** 默认 `--allow-added "相关,延伸问题"`，靠 `## 相关` 小节整段剔除来避免误判；新增的其它结构（比如自己加的「延伸问题」小节）要在 `--allow-added` 里显式列出，否则会被判成改写。
- **`30_Sources/` 存档会报 `title-mismatch`（WARN，属正常）。** 存档保留原文的第一个 H1，而 frontmatter 的 `title` 要跟文件名一致（例如文件名 `面试题库原文.md`、H1 仍是 `# 面试题库`），两者天然不同——这条警告是结构性的，别为了消警告去改存档正文。
- **`roundtrip_check.py` 的「未关联存档的笔记」只统计带 frontmatter 的文件**，根 `README.md` 之类不计数。
- **校验器只查结构，不判断内容真假。** 0 错误 ≠ 笔记写对了，事实与措辞仍要人看。
- **拆分只认标题结构。** 长表格、时间线、代码清单不适合按 `##` 拆，技能会保留原样而不是硬拆。
- **大小写按 Linux 语义处理**：`[[Guide]]` 不匹配 `guide.md`。Windows/macOS 上 Obsidian 容忍大小写不一致，校验器不容忍——这是故意的，跨平台库只能取严格侧。
- **正文是否被动过，现在有两道取证**：`roundtrip_check.py`（逐篇连续子序列）和 git `rename` 相似度。 `rename` 相似度、整体回滚都依赖 git；库不在版本控制里时，先 `git init` 并提交一次基线，再让 agent 动手。
- **平台坑主要是 Windows 的**（沙箱写入需提权、PowerShell 变量大小写不敏感、`Select-Object -First` 掐断上游管道、UTF-8 需无 BOM）。校验器本身跨平台，用 Python 3 标准库，无第三方依赖。

## 维护须知

- 改了 frontmatter 字段或目录结构，必须同步改 `scripts/validate_vault.py` 里的 `REQUIRED_FIELDS` / `DIR_TYPE` / `VALID_TYPES`，否则会误报；改完跑 `python tests/run_eval.py`。
- 新增/删除校验规则 id 时，`tests/run_eval.py` 里的预期规则集合要跟着改（断言是 SUPERSET，新增规则不会误伤，删规则会 FAIL）。
- `README.md` 和 `SKILL.md` 描述的是同一个东西，改流程时两边都要动，别只改一边。
- `SKILL.md` 的 `description` 只写「何时触发」，不要写流程摘要——实测把流程写进描述会让 agent 跳过正文直接干。
- 新增平台坑时写进 SKILL.md 的「平台坑」一节，并注明是哪个平台实测的，不要凭想象补。

## 许可

[MIT](LICENSE)
