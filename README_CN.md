# claude_encoding_guard (chardet 7.x 实验版)

> [!WARNING]
> **这是 `chardet7-preview` 实验分支。** 稳定版本在 [`main`](https://github.com/ymonster/claude_encoding_guard/tree/main) 分支。本分支仅用于测试 chardet 7.x 迁移或贡献反馈。

Claude Code 编辑文件时，自动保留非 UTF-8 编码和行尾符。

[English](README.md)

## 与 `main` 的差异

| 维度 | `main` (5.x) | `chardet7-preview` |
|---|---|---|
| chardet 版本 | `>=5,<6` (LGPL-2.1+) | `>=7,<8` (0BSD) |
| `binaryornot` 依赖 | 必需 | **去除** —— chardet 7.x 内置 stage 5 binary detection |
| 置信度阈值 | 每编码 `>= 0.5` | `>= 0.10` 噪声底线（7.x cosine 几何评分，非贝叶斯） |
| ASCII 处理 | chardet 5.x 自然识别 | 显式短路（7.x 把 ASCII 报为 Windows-1252） |
| `STRUCTURAL_TRUSTED` 旁路 | 有（binaryornot 对短 CJK / 混合 Cyrillic 误判） | 不需要（stage 5 无类似 FP） |
| codec 名 | `gbk` / `shift_jis` / `euc-kr` | `GB18030` / `cp932` / `CP949`（superset codec，round-trip 更宽容） |
| 测试 harness 结果（89 fixtures，含中欧） | 79/89（10 个已知 5.x 误检） | 88/89（1 个 50 字节 cp1250 fixture 保守跳过） |

详见 [TECHNICAL.md](TECHNICAL.md) → "chardet 7.x Migration Findings"。

## 安装（实验分支）

Claude Code plugin marketplace 不支持指定 GitHub 源的分支，需要手动安装：

```bash
git clone -b chardet7-preview https://github.com/ymonster/claude_encoding_guard
```

然后在项目的 `.claude/settings.local.json` 里指向克隆出来的 `hooks/encoding_guard.py`：

```json
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "Read|Edit|Write",
      "hooks": [{
        "type": "command",
        "command": "uv run --script <clone-path>/hooks/encoding_guard.py pre"
      }]
    }],
    "PostToolUse": [{
      "matcher": "Edit|Write",
      "hooks": [{
        "type": "command",
        "command": "uv run --script <clone-path>/hooks/encoding_guard.py post"
      }]
    }],
    "Stop": [{
      "hooks": [{
        "type": "command",
        "command": "uv run --script <clone-path>/hooks/encoding_guard.py restore-all"
      }]
    }]
  }
}
```

## 问题

Claude Code 的 Edit/Write 工具始终输出 UTF-8 + LF 行尾。编辑 GBK、Big5、Shift_JIS 等编码的文件时，原始编码会被静默破坏；Windows 上 CRLF 行尾也会丢失。官方回应是 ["not planned"](https://github.com/anthropics/claude-code/issues/12203)。

相关 issue：[#6485](https://github.com/anthropics/claude-code/issues/6485)、[#7134](https://github.com/anthropics/claude-code/issues/7134)、[#28523](https://github.com/anthropics/claude-code/issues/28523)、[#38887](https://github.com/anthropics/claude-code/issues/38887)。

## 工作原理

```
PreToolUse (Read)                       PostToolUse (Edit/Write)
    │                                        │
    ├─ ASCII 短路                            ├─ 读取缓存的编码 + 行尾风格
    ├─ 检测编码 (chardet 7.x)                ├─ UTF-8 → 原始编码
    │   ├─ stage 5 binary detection         ├─ 恢复行尾符
    │   ├─ era=MODERN_WEB|LEGACY_ISO        ├─ 删除会话缓存
    │   └─ prefer_superset=False            └─ 完成
    ├─ 检测行尾 (CRLF/LF)
    ├─ 原始编码 → UTF-8
    ├─ 保存会话缓存
    └─ Claude 读到正确内容
```

编码转换发生在 **Read 阶段**，在 Claude Code 将文件加载到内存之前。这是关键——Claude Code 会把非 UTF-8 字节当作 UTF-8 解释，无效序列变成 U+FFFD（不可逆）。先转成 UTF-8，Claude 就能看到正确内容并正常编辑。

## 功能

- **编码保护**：GBK、GB2312、GB18030、Big5、Big5-HKSCS、EUC-TW、Shift_JIS、EUC-JP、ISO-2022-JP、EUC-KR、Windows-1252、Windows-1251（西里尔文）、Windows-1250（中欧）、ISO-8859-1
- **行尾保护**：Claude Code 将 CRLF 转为 LF 后自动恢复
- **二进制文件防护**：chardet 7.x 内置 stage 5 binary detection（始终开启）
- **会话隔离**：多个 Claude Code 会话互不干扰
- **零配置**：安装即用

### 验证

按上面 "安装（实验分支）" 步骤接好 hooks 后，Read 任意非 UTF-8 文件——中文应正确显示而非乱码。

## 设计决策

- **Read 阶段转换**：Claude Code v2.1.90+ 对 hook 修改的文件不重新读取内容。Edit 阶段转换会导致 U+FFFD 覆盖。Read 阶段转换确保 Claude 第一次看到的就是正确 UTF-8。
- **`uv run --script`**：普通 `uv run` 触发项目 sync 会关闭 Windows 上的 stdin 管道。`--script` 跳过项目发现。
- **chardet 7.x**：置信度模型重写为 cosine similarity 评分（不再是"我有多确定"，而是"判定路径有多硬"）。GB18030 由于 bigram 集合最大，几何上 confidence 永远在 0.14–0.25，与文件大小无关——所以我们用 0.10 噪声底线 + 白名单过滤，而非高置信度阈值。详见 [TECHNICAL.md](TECHNICAL.md)。
- **二进制检测**：chardet 7.x pipeline stage 5 在非文本输入上返回 `encoding=None`，confidence 0.95–1.00，替代了 `main` 分支的 binaryornot 决策树。测试 fixture 集上未观察到误判。
- **编码别名**：GB2312 → GBK（字节兼容超集），ISO-8859-1 → Windows-1252（行业惯例）。
- **会话隔离缓存**：`<tmpdir>/.cc_encoding_cache/<session_id>/`——不跨会话干扰。Hook 严格只操作自己会话的记录；超过 24 小时的**空**会话目录自动清除，含记录的目录永不自动删除（见"崩溃恢复"）。

## 已知限制

- **二进制文件误编辑的兜底建议。** 我们已经尽量通过各种手段规避对二进制文件的编辑（绝大多数场景下 Claude Code 不会去编辑一个 binary 文件），但总是有可能有疏漏。考虑过用 local git 自动 stash 做兜底，但担心会影响用户自己的 git workflow（污染 stash list、reflog 等）。所以这里强烈建议在 git 或其他版本控制工具下使用本插件，一旦真出现了问题保证可以恢复。

- **Windows-1252 stale cache 边界情况。** 如果缓存删除失败（如杀毒软件锁定）且文件原始 Windows-1252 字节恰好是合法 UTF-8，残留缓存无法自愈。此情况需要两个极端条件同时满足，不影响 CJK 编码（GBK/Big5/Shift_JIS 字节不是合法 UTF-8）。

- **混合行尾。** 同时包含 CRLF 和 LF 的文件会恢复为主要风格。

- **依赖绝对路径。** 本插件依赖 Claude Code 在 hook stdin JSON 中提供绝对路径（已验证的实际行为）。符号链接或 junction 指向同一文件可能产生不同的缓存键。

- **多个 Claude Code 实例同时操作同一文件。** 两个 CC 实例并发编辑/读取同一文件时，一个会话 Stop 触发的恢复可能覆盖另一会话尚未完成的 Edit。会话隔离缓存阻止了大部分交叉污染，但仍有一个极窄的竞态窗口。并发使用极少见，这个 trade-off 被接受以换取 Read-without-Edit 的恢复能力。`/encoding-guard:scan` 同理：它只在你主动运行时执行，且无法判断另一个窗口里的 session 是否还活着——使用前请先关闭同一项目下的其他 Claude Code 实例。

## 崩溃恢复

Claude Code 中途被杀（崩溃、终端被关）时，文件可能停留在临时的 UTF-8 状态。四层防线：

| 层 | 时机 | 作用 |
|---|---|---|
| `Stop` restore-all | 每轮对话结束 | 恢复本轮读过但没编辑的文件 |
| `SessionEnd` restore-all | 会话退出 | 兜底——含被 Esc 打断的轮次 |
| `SessionStart(resume)` restore-all | resume 会话时 | 在 resume 后任何动作之前清理崩溃残留 |
| `/encoding-guard:scan` | 手动运行 | 找出再也没 resume 的崩溃会话留下的记录 |

前三层是内建机制——resume 崩溃的会话（CLI `--resume` / `-c` 或 TUI 的 `/resume` 选单都保持 session ID）即可按照规则进行恢复。但如果这个session从来没有resume过，就可能出现未被恢复的 UTF-8 文件或者未被清理的缓存记录。那么针对当前目录，可以使用`/encoding-guard:scan`进行扫描。

每次转换和恢复都会写入日志（含触发它的 hook 事件名）。日志位于 `<tmpdir>/.cc_encoding_cache/encoding_guard.log`，其中 `<tmpdir>` 是系统临时目录：Windows 上通常是 `C:\Users\<用户名>\AppData\Local\Temp`（即 `%TEMP%`），Linux/macOS 上通常是 `/tmp`。如果想确认自己机器上的实际位置，可以运行 `python -c "import tempfile; print(tempfile.gettempdir())"`。

## `/encoding-guard:scan` 命令

这是插件目前提供的唯一一个命令：

```
/encoding-guard:scan
```

它只检查插件自己的缓存记录，找出**当前目录下**被留在 UTF-8 转换态的文件以及留存的缓存记录，并给出报告或选择。记录分两类：

- **可恢复** —— 文件还在、还是 UTF-8 态，凭记录可以转回原编码
- **已作废** —— 文件已删除、已经回到原编码、或记录本身损坏，这些会被静默清理（特指清理掉缓存记录）

一切干净就直接告诉你并结束。发现可恢复的文件时，展示清单（含每条记录的存在时长）并询问怎么处理：

- **Restore all**（全部恢复）—— 把每个文件转回原编码
- **Discard all records**（放弃全部记录）—— 文件保持 UTF-8，删除记录（指插件生成的缓存记录）。注意：记录是唯一记得"这个文件原本是什么编码"的地方，删了就永久无法再恢复
- **Pick per file**（逐个决定）—— 并排出现两栏勾选：**Restore**（勾要恢复的）和 **Discard**（勾要放弃的）。两边都不勾 = 这次不动它，记录保留，下次运行还会再问；两边都勾 = 按恢复处理
- **Not now**（暂不处理）—— 什么都不动

恢复结果逐条报告。恢复失败（通常是文件在 UTF-8 期间被编辑进了原编码无法表示的字符，比如 GBK 文件里进了 emoji）会给出原因，记录保留——此时的出路只剩放弃记录，或自己手工处理内容后再试。

安全规则：属于你当前所在会话的记录自动跳过（本会话自己的 hook 会处理它们）。每条记录都展示存在时长，这可能会提醒你是否当前有其它会话在处理同一个路径下的文件。

命令背后由脚本的几个 mode（`scan` / `scan-current` / `restore-current` / `prune-current`）驱动，属于内部实现，面向调试和开发的说明在 [TECHNICAL.md](TECHNICAL.md)。

## 高级配置

通过 `if` 字段限制 hook 仅对特定扩展名生效（Claude Code v2.1.85+），在项目的 `.claude/settings.local.json` 中配置：

```json
{
  "matcher": "Read|Edit|Write",
  "if": "tool_input.file_path MATCHES '\\.(?:h|c|cpp|txt|xml|csv|ini)$'",
  "hooks": [...]
}
```

不配置 `if` 时，hook 对所有文件操作生效。性能开销很小——chardet 对 UTF-8/ASCII 文件几乎立即跳过。

## 技术细节

参见 [TECHNICAL.md](TECHNICAL.md)，包括平台差异（Windows stdin、codepage、CRLF）、chardet 版本敏感性、二进制检测原理、缓存设计，以及转换策略的演进过程。

## 致谢

感谢 [@lbresler](https://github.com/lbresler) 贡献的 [#1](https://github.com/ymonster/claude_encoding_guard/pull/1)（修复 ISO-8859-1 / GB2312 的 alias-dash 不匹配问题）和 [#2](https://github.com/ymonster/claude_encoding_guard/pull/2)（`handle_restore_all` + Stop hook 恢复 Read-without-Edit 场景）。

感谢 [@Pacman766](https://github.com/Pacman766) 贡献的 [#3](https://github.com/ymonster/claude_encoding_guard/pull/3)（Windows-1251 / CP1251 西里尔文编码支持）。

感谢 [@albert-polak](https://github.com/albert-polak) 贡献的 [#4](https://github.com/ymonster/claude_encoding_guard/pull/4)（Windows-1250 / CP1250 中欧编码支持）。

## License

MIT
