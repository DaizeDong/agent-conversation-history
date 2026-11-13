# agent-conversation-history

从 Claude Code 转录重建对话，导出为 Markdown，从选定节点分叉出新会话，或在内置的本地小网页里浏览和整理会话。

[![Python Library](https://img.shields.io/badge/Python-Library%20%2B%20CLI-orange?style=flat)](src/agent_conversation_history/__init__.py)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-green?style=flat)](pyproject.toml)
[![Languages](https://img.shields.io/badge/Languages-EN%20%2F%20CN-blue?style=flat)](#语言)
[![Roadmap](https://img.shields.io/badge/Roadmap-v0.3.0-purple?style=flat)](ROADMAP.md)

[English](README.md) | [中文版](README_CN.md)

## 设计理念

重建对话需要处理原调查在转录中观测到的三种结构，单独沿 `parentUuid` 上溯无法完整处理：

1. **一次助手回复按内容块拆成好几行。** 并行工具调用会让一个节点看上去有两个孩子(同一 `message.id` 的下一块,和第一块的工具结果)。那是一次回复,不是分支。这个库按 `message.id` 把回复归组,并给每个 `tool_use` 配上恰好一个结果。
2. **压缩边界那一行的 `parentUuid` 是 null,** 朴素上溯走到这里就停了,前面整段历史不在链上。它的 `logicalParentUuid` 常常指向边界之后写下的行,照着走会绕回来。前驱按三级规则选,其中唯一靠猜的那一级会明说是猜的。
3. **被保留的消息留在它们写下的原位,也就是边界之前,** 只由边界的 `compactMetadata` 把它们接进新上下文。部分分叉(`full=False`)照 Claude Code 加载时的方式重链,分叉出来的文件里恰好是模型在那个节点看到的上下文。默认的分叉则带上整段历史,经过的压缩边界都保留。

会话根目录由调用方显式传入 `root`，库没有默认目录。分叉会独占创建新文件；重命名和迁移则通过带恢复记录的文件事务执行。写入位置位于 git 工作树内时会拒绝操作。

## 适用范围

零依赖 Python 库和 CLI 通过一次扫描建立字节偏移索引。内存中每行只保留紧凑记录，完整内容按需读取，避免每次浏览都完整加载数百 MB 的转录。进程内 LRU 缓存保留三份索引，以路径、mtime 和大小为键。

库按单个会话工作。`agent-conversation-history serve` 在它之上提供一个最小的网页界面,用来列出、阅读、导出、分叉和整理会话;更大的应用仍可以导入库,自带界面。

## 安装

```
pip install "agent-conversation-history @ git+https://github.com/DaizeDong/agent-conversation-history"
```

或者在检出目录里 `pip install .`。需要 Python 3.10 或更新。wheel 是 `py3-none-any`,没有依赖。

## 快速上手

```
agent-conversation-history chain  SID [--leaf U] [--sub AGENT]
agent-conversation-history node   SID UUID [--sub AGENT]
agent-conversation-history export SID --to U [--from U] [--leaf U] [--sub AGENT]
                                  [--tools] [--thinking] [--out FILE] [--this-file-only]
agent-conversation-history fork   SID AT [--leaf U] [--id UUID] [--title-suffix S] [--partial]
agent-conversation-history inline (SID | --all) [--sessions-dir DIR]
agent-conversation-history serve  [--host H] [--port N] [--sessions-dir DIR] [--export-dir DIR]
                                  [--allowed-host H] [--allow-remote] [--log-header NAME]
agent-conversation-history --version
```

每个子命令都带 `--root DIR`。没给时命令行依次用 `AGENT_CONVERSATION_HISTORY_ROOT`、`$CLAUDE_CONFIG_DIR/projects`、`~/.claude/projects`。`AT` 可以是节点 uuid、`leaf`(当前末端)、`last-human`(最后一条用户消息)、`before-last-human`(它之前的那个节点,也就是那条消息发出之前的状态)或 `before:UUID`(该节点的父节点)。`fork --id UUID` 指定新会话的 id 并且幂等:这个会话已存在就什么都不写,带 `reused: true` 返回已有的结果。分叉结果里有 `newId`、`file`、`cwd`、`command`(`claude --resume` 那一行)、`title`、`at`、`forkedFrom` 和 `warnings`。

退出码:`0` 成功,`1` 被拒(stdout 上是 `{"error": {"code", "message"}}`),`2` 用法错误,`3` 未检查(没有可用的根目录),`4` 内部故障(stdout 上是 `{"error": {"code": "internal", "message": <异常类名>}}`,说明是程序缺陷或文件读不了,不是拒绝)。显式给了 `--root`,哪怕是 `--root ""`,也绝不会被 `AGENT_CONVERSATION_HISTORY_ROOT` 顶替。`export --out` 把 Markdown 独占创建写进文件,目标在 git 工作树里就拒绝,理由和 `fork` 一样。`python -m agent_conversation_history` 用法相同。

CLI 每次调用都重建索引。长期运行的调用方应该直接导入这个库,让缓存在请求之间留住。

## 分叉与更早的历史

Claude Code 写出的分叉或续写会话有两种形态:要么新文件带着全部历史,要么它从某个压缩边界开头,边界之前的历史只在父会话的文件里。第二种形态下,删掉父会话就等于悄悄删掉子会话的前半段。

`fork` 现在把显示链整段复制到分叉点,经过的每个压缩边界都保留,分叉出来的会话自带历史。`--partial`(`full=False`)是老行为,只带最近一次压缩之后模型看到的部分。

已经是第二种形态的会话,靠两份文件共有的 uuid 找到父会话和分叉点。`export_full`(CLI `export` 的默认,`--this-file-only` 关掉)导出时把父会话更早的历史接在前面。`inline_ancestors`(CLI `inline`)把这段历史写进子会话自己的文件:插在第一条链条目之前,原来的每个字节和修改时间都不变,新文件重新建索引并核对叶子没变后才原子替换,开着的会话一律拒绝,因为 Claude Code 持有那个文件的追加句柄。写完之后父会话就可以删。插入的行在一个 `parentUuid` 为 null 的压缩边界之前,Claude Code 恢复会话时上溯到那里就停。

## 网页界面

```
agent-conversation-history serve --root ~/.claude/projects
```

起一个零依赖的本地服务(默认 `127.0.0.1:8765`):项目和会话列表、按轮阅读、导出 Markdown(下载或存到服务器)、重命名、分叉、带恢复的回收站,以及 Claude Code 是否还开着这个会话的标记(最近有对话是绿色,窗口开着但没动静则是黄色并写出空闲多久)。删除会话是挪进回收站项目;如果别的会话的更早历史只在它里面,会先把那段历史写进那个会话,而那个会话开着时拒绝删除。

它没有登录。默认只绑回环地址,除非给 `--allow-remote`;只接受回环 `Host` 头加上 `--allowed-host` 指定的;写操作只收 `application/json` 且 `Origin` 要和 `Host` 一致;导出文件权限 0600。要对外就放在你自己的认证反向代理后面。`--sessions-dir` 指向 Claude Code 的活跃会话登记目录(默认 `~/.claude/sessions`),`--export-dir` 是导出目录(默认 `~/agent-conversation-history-exports`,可以写 `%Y-%m` 这类 strftime 代码),`--log-header` 指定一个请求头,其值会和写操作一起记进日志。

## 被其它项目调用

别的程序可以把它当普通子进程用,不需要安装:检出(或作为 submodule 加进去),然后运行 `PYTHONPATH=<检出目录>/src python3 -m agent_conversation_history ...`。需要 Python 3.10 或更新,没有依赖。下面这些是稳定接口,改动其中任何一项都会在 CHANGELOG 里记为破坏性变更,并通知调用它的项目:

- **可用性探测。** `--version` 退出码为 `0`,并打印 `agent-conversation-history <版本>`。调用方只想知道工具能不能用时,只看这个退出码。
- **输出。** 除 `serve` 外的子命令成功时 stdout 上只有一个 JSON 对象。退出码:`0` 成功,`1` 被拒(`{"error": {"code", "message"}}`),`2` 用法错误,`3` 没有可用的根目录,`4` 内部故障。被拒的 `code` 取值是稳定的;调用方应该按 `not_on_path`、`no_parent`、`bad_uuid`、`unavailable` 分支,不要解析 message 文本。
- **`fork SID AT`。** `AT` 是节点 uuid、`leaf`、`last-human`、`before-last-human` 或 `before:UUID`。选项:`--root DIR`、`--leaf U`、`--id UUID`(幂等:会话已存在就带 `reused: true` 返回已有结果,什么都不写)、`--title-suffix S`、`--partial`。结果字段:`newId`、`file`、`cwd`、`command`、`title`、`at`、`forkedFrom`(`sessionId`、`messageUuid`)、`reused`、`lines`、`emitted`、`leafUuid`、`warnings`。
- **根目录。** `--root`,否则 `AGENT_CONVERSATION_HISTORY_ROOT`,否则 `$CLAUDE_CONFIG_DIR/projects`,否则 `~/.claude/projects`。Claude Code 用的配置目录不是默认值的调用方,请显式传 `--root`。

分叉只会写一个新的会话文件,源会话和正在运行的 Claude Code 窗口都不动,所以调用方用返回的 `command` 去恢复这个分叉。

## 数据放在哪

转录留在调用方传入的仓外根目录下。分叉是在源转录自己的项目目录里新建一份 `<uuid>.jsonl`。导出写到 `--out` 指定的地方,或者在内存里交还调用方。`.gitignore` 在全仓范围排除 `*.jsonl` 和 `*.jsonl.gz`。怎么核对的,写在 `.dataclass.json` 里。

## 会话操作与恢复

`rename(sid, title, root=...)` 追加原生标题记录并更新索引；`move(sid, target_project, root=...)`
保留转录字节和会话 ID，迁移完整子目录并更新两边索引。目标必须是根目录内已有的项目目录。
正在写入的文件、同名冲突、链接和跨卷移动会被拒绝。恢复记录放在调用方根目录的
`.agent-conversation-history-ops` 下，`recover_pending(root)` 可回退中断的操作，后续修改也会先执行恢复。
根锁协调本库的调用方，文件占用和前后状态检查用于发现其他程序的干扰；移动前应关闭写入该会话的程序。
`fork` 可接收 UUID 形式的 `request_id`，响应丢失后沿用它重试会返回同一份新会话。
恢复命令优先采用存储目录的元数据，不改写历史消息中的工作目录。

`delete_plan(sid, root=..., expected_project=...)` 返回将删除的文件数、字节数和原生索引条目数。
调用方确认范围后，将相同的会话、项目、预览 `fingerprint` 和 UUID 形式的 `request_id` 传给 `delete`。
它永久删除转录和关联子目录，保留项目目录及其他会话。预览后的文件变化会导致拒绝；重试沿用原请求，
不会删除同 ID 重新创建的会话。提交前中断可回退，提交后中断会继续清理。
`cleanup_pending` 表示部分文件尚未清理，应重试同一请求。这两项目前通过库 API 提供。

## 局限

索引以文件为单位。祖先会话的旁挂文件(存在转录旁边的大型工具结果)在补全或分叉时不会被复制。恢复命令采用明确的项目元数据或与存储目录匹配的历史目录；信息缺失时会提示。
Windows 文件事务需要 Windows 10 或更新系统，其他平台需要支持原子且不覆盖目标的重命名操作。

## 语言

这份 README 有 [English](README.md) 和 [中文](README_CN.md) 两个版本,逐节对应。代码注释和库返回的消息是中文。

## 路线图 · 贡献 · 许可

见 [ROADMAP.md](ROADMAP.md) 和 [CHANGELOG.md](CHANGELOG.md)。欢迎提 issue 和 pull request,其中的每一个例子都必须是合成数据。MIT 许可,见 [LICENSE](LICENSE)。
