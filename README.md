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

直接说人话即可，`description` 里的触发词覆盖「整理 / 重构 / 规范化 / 拆分 / 建知识库 / 归档」：

- 「把这个目录整理成 Obsidian 知识库：`E:\notes`」
- 「`面试题.md` 有 3000 行，拆成一个技术点一篇」
- 「库里的 frontmatter 和双链不规范，帮我规范化一下」

### 3. 过程

agent 会分五步走。**第 2 步会先给出「旧路径 → 新路径」映射表等你确认**；旧文件只有在副本校验通过、并且你点头之后才会被删除（第 5 步）：

```
盘点 → 设计映射 → 机械拆分 → 建链接 → 校验 → 安装 → 提交
```

### 4. 校验与验收

```bash
python scripts/validate_vault.py <vault_root>
```

必须 **0 错误** 才能安装。安装后自查：

```bash
git show --stat        # 原文件应显示为 rename，相似度 >90%；过低说明正文被改了
git revert <commit>    # 不满意就整体回滚
```

## 仓库结构

```
obsidian-vault-builder/
├── SKILL.md                      # 技能本体，agent 读这个
├── README.md                     # 本文件，给人看
├── scripts/
│   └── validate_vault.py         # 零依赖校验器（Python 3 标准库）
└── templates/
    ├── 原子笔记.md                # 一个概念一篇
    ├── 问答型笔记.md              # 面试题 / 故障复盘（已含标题降级后的层级）
    ├── 来源笔记.md                # 原文存档
    ├── MOC.md                     # 地图笔记
    └── vault-AGENTS.md           # 库内规范骨架，落地时复制成目标库根目录的 AGENTS.md
```

`templates/vault-AGENTS.md` **故意不叫 `AGENTS.md`**：任何目录下的 `AGENTS.md` 都会被 harness 自动加载成指令，放在技能目录里会污染当前会话（实测已触发过一次）。

不捆绑「一键拆分脚本」：每篇长文的标题结构都不同，泛化脚本比手工拆分更脆。拆分规则写在 [SKILL.md](SKILL.md) 第 3 步。

## 校验器

```bash
python scripts/validate_vault.py <vault_root>          # 完整输出
python scripts/validate_vault.py <vault_root> --quiet  # 只输出结论
```

退出码：`0` 无错误（警告允许）· `1` 有错误 · `2` 用法错误。可直接接进 CI，也可以脱离技能单独使用。

### 检查项

**ERROR**

- 缺少 frontmatter，或缺 8 个必需字段：`title type status tags created updated source confidence`
- `type` / `status` / `confidence` 取值不在枚举内
- `type` 与所在目录前缀矛盾（`10_Notes`→`note`，`20_MOC`→`moc`，`30_Sources`→`source`，`40_Projects`→`project`，`90_Journal`→`journal`）
- 失效双链 `[[X]]`、失效相对链接 `[t](path.md)`
- 标题层级跳级（`#` 下直接出现 `###`）
- `type: note` / `project` 的正文**零出链**（`source:` 字段里的双链不算）

**WARN**

- 孤儿笔记（零入链）
- 正文只有 1 条出链（规范要求 ≥ 2）
- `title` 与文件名不一致
- 文件名含空格
- `status: evergreen` 但没有任何入链

### 四个容易误判的判定细节

1. **围栏代码块先被剥离再扫链接**，否则文档里举例的 `[[相关笔记 A]]` 会被当成失效链接。
2. **行内代码不会被剥离。** 写成 `` `[[X]]` `` 的示例仍会被当作真实链接；写链接示例请用围栏代码块。
3. **指向代码仓库的锚点不算死链。** `../src/main/java/...`、`Foo.java:12-34` 会被单独汇总成「代码仓库锚点」，属正常情况——它们指向的不是本库文件。校验器不会要求你去修它们，**也不要去修**。
4. `00_Inbox/` 和 `99_Archive/` 豁免出链检查（未加工输入与归档件不需要自己长链接）。

## 实测效果

同一份 fixture（8 个文件 / 6 篇 md，含 314 行长文、一个故意写错的旧 `AGENTS.md`、一处失效相对链接），两种条件各跑一次：

| 维度 | 无技能 | 有技能 |
|---|---|---|
| 校验结果 | 49 错误 / 8 警告 | **0 错误 / 0 警告** |
| 原长文 | 删除，只剩拆出的 5 篇 | `30_Sources/` 存档，正文字符数与原文逐一相等 |
| 正文完整性 | 结构折叠 + 加粗改写 | 反向还原后与原文逐行相等 |
| frontmatter | 自创 9 字段 | 固定 8 字段，`source` 双链指回存档 |
| 失效链接 | 凭空生成「架构说明」 | 改纯文本 + 注明 + MOC 登记缺口 |
| 旧 AGENTS.md | 直接覆盖 | 归档保留 |
| 标题层级 | `#` 下跳 `###` | `#` → `##` → `###` 逐级下降 |

第一轮带技能的运行仍然出现了「拆出的笔记正文零出链」——因为最初的校验器只查入链不查出链。补上规则、把「自己写 `## 相关`，别指望 MOC 兜底」写进流程后，第二轮才真正 0 错误。**这类漏洞不跑测试是发现不了的。**

## 已知限制

- **增量分支没有对照测试。** 「收件箱单条输入 → 落一篇笔记」的流程和边界都写了，但只验证过存量库重构。
- **校验器只查结构，不判断内容真假。** 0 错误 ≠ 笔记写对了，事实与措辞仍要人看。
- **拆分只认标题结构。** 长表格、时间线、代码清单不适合按 `##` 拆，技能会保留原样而不是硬拆。
- **正文是否被动过，只能靠 git 取证。** `rename` 相似度、整体回滚都依赖 git；库不在版本控制里时，先 `git init` 并提交一次基线，再让 agent 动手。
- **平台坑主要是 Windows 的**（沙箱写入需提权、PowerShell 变量大小写不敏感、`Select-Object -First` 掐断上游管道、UTF-8 需无 BOM）。校验器本身跨平台，用 Python 3 标准库，无第三方依赖。

## 维护须知

- 改了 frontmatter 字段或目录结构，必须同步改 `scripts/validate_vault.py` 里的 `REQUIRED_FIELDS` / `DIR_TYPE` / `VALID_TYPES`，否则会误报。
- `README.md` 和 `SKILL.md` 描述的是同一个东西，改流程时两边都要动，别只改一边。
- `SKILL.md` 的 `description` 只写「何时触发」，不要写流程摘要——实测把流程写进描述会让 agent 跳过正文直接干。
- 新增平台坑时写进 SKILL.md 的「平台坑」一节，并注明是哪个平台实测的，不要凭想象补。

## 许可

[MIT](LICENSE)
