# convo-chain

Reconstruct a Claude Code conversation from its transcript, export it to Markdown, fork it at a selected node, or browse and tidy your sessions in a small built-in local web UI.

[![Python Library](https://img.shields.io/badge/Python-Library%20%2B%20CLI-orange?style=flat)](src/convo_chain/__init__.py)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-green?style=flat)](pyproject.toml)
[![Languages](https://img.shields.io/badge/Languages-EN%20%2F%20CN-blue?style=flat)](#languages)
[![Roadmap](https://img.shields.io/badge/Roadmap-v0.3.0-purple?style=flat)](ROADMAP.md)

[English](README.md) | [中文版](README_CN.md)

## Design philosophy

Conversation reconstruction must account for three transcript structures observed during the original investigation; following only `parentUuid` does not handle them:

1. **One assistant reply is split into one line per content block.** Parallel tool calls make a node look like it has two children (the next block of the same `message.id`, and the first block's tool result). That is one reply, not a branch. This library groups replies by `message.id` and pairs every `tool_use` with exactly one result.
2. **A compaction boundary has `parentUuid: null`,** so a naive walk stops there and the whole history before it vanishes. Its `logicalParentUuid` often points at a line written after the boundary, so following it loops back. The predecessor is chosen by a three level rule, and the one level that is a guess is reported as a guess.
3. **Preserved messages stay where they were written, before the boundary,** and only the boundary's `compactMetadata` links them into the new context. A partial fork (`full=False`) relinks them the way Claude Code does at load time, so the forked file holds exactly the context the model saw at that node. The default fork instead keeps the whole history, every compaction boundary included.

The caller passes an explicit session `root`; the library never guesses a default directory. Fork creates a new file exclusively. Rename and move are explicit metadata and file operations with recovery journals. Writes are refused inside git worktrees.

## Scope

The dependency-free Python library and CLI index each transcript in one pass using byte offsets. Memory holds a compact record per line; full content is read on demand. This avoids loading a several-hundred-megabyte transcript in full for each browse operation. An in-process LRU cache holds three indexes, keyed by path, mtime and size.

The library operates on one session at a time. `convo-chain serve` adds a minimal web UI on top of it for listing, reading, exporting, forking and tidying sessions; a larger application can still embed the library and bring its own interface.

## Install

```
pip install "convo-chain @ git+https://github.com/DaizeDong/convo-chain"
```

Or from a checkout: `pip install .`. Python 3.11 or newer. The wheel is `py3-none-any` and has no dependencies.

## Quick start

```
convo-chain chain 00000000-0000-4000-8000-000000000000 --root C:/Users/you/.claude/projects
```

`--root` is the directory that holds one folder per project, each with `<session-id>.jsonl` files. Set `CONVO_CHAIN_ROOT` instead of passing it every time. Output is JSON on stdout.

## Library API

```python
import convo_chain as cc

root = "C:/Users/you/.claude/projects"          # always passed in, never read from the environment
cc.shape(sid, leaf=leaf)                          # id shape gate, raises before any filesystem access
r = cc.chain(sid, leaf=None, sub=None, root=root) # the display chain cut into turns, with forks
n = cc.node(sid, uuid, sub=None, root=root)       # one node's full content, read back by offset
md = cc.export_md(sid, to=uuid, frm=None, include_tools=False, include_thinking=False, root=root)
f = cc.fork(sid, at=uuid, leaf=None, root=root)   # writes <newId>.jsonl next to the source, whole history
md = cc.export_full(sid, to=uuid, root=root)      # export_md plus the parent session's earlier history
cc.inline_ancestors(sid, root=root, is_live=None) # write that earlier history into the session's own file
print(f["command"])                               # cd '<cwd>'; claude --resume <newId>
```

Also exported: `dependents(sid, root)`, `rename(sid, title, root=...)`, `move(sid, target_project, root=...)`, `delete_plan`, `delete`, `project_info`, `recover_pending`, `locate`, `resume_command`, `clear_cache`, `CACHE_SLOTS`, and the transcript rules `typed_text(entry)` and `looks_injected(text)`.

Errors come in two kinds that do not inherit from each other. `ConvoChainError` is a refused request and carries a stable `.code` (`bad_id`, `bad_sub`, `bad_leaf`, `bad_uuid`, `not_found`, `ambiguous`, `outside_root`, `not_on_path`, `bad_range`, `stale_index`, `exists`, `inside_repo`, `unrelinkable`, `empty_fork`, `no_sub_fork`, `unavailable`, and a few more). `Unavailable` means no usable root: `chain` and `node` return `{"available": false, "reason": ...}` for it, while `export_md` and `fork` raise `ConvoChainError` with code `unavailable`.

## CLI

```
convo-chain chain  SID [--leaf U] [--sub AGENT]                  [--root DIR]
convo-chain node   SID UUID [--sub AGENT]                        [--root DIR]
convo-chain export SID --to U [--from U] [--leaf U] [--sub AGENT]
                   [--tools] [--thinking] [--out FILE]           [--root DIR]
convo-chain fork   SID AT [--leaf U] [--partial]                 [--root DIR]
convo-chain inline [SID | --all] [--sessions-dir DIR]            [--root DIR]
convo-chain serve  [--host H] [--port N] [--sessions-dir DIR] [--export-dir DIR]
                   [--allowed-host H] [--allow-remote] [--log-header NAME] [--root DIR]
convo-chain --version
```

Exit codes: `0` success, `1` refused (`{"error": {"code", "message"}}` on stdout), `2` usage error, `3` not checked (no usable root), `4` internal failure (`{"error": {"code": "internal", "message": <exception class name>}}` on stdout, a bug or an unreadable file rather than a refusal). An explicit `--root`, even `--root ""`, is never replaced by `CONVO_CHAIN_ROOT`. `export --out` writes the Markdown to a file with exclusive create and refuses a target inside a git work tree, for the same reason `fork` does. `python -m convo_chain` works the same way.

The CLI rebuilds the index on every call. A long lived caller should import the library so the cache survives between requests.

## Forks and earlier history

Claude Code writes a forked or resumed session in one of two shapes. Either the new file carries the whole history, or it starts at a compaction boundary and the history before that boundary lives only in the parent's file. In the second shape, deleting the parent silently deletes half of the child.

`fork` now copies the whole display chain up to the fork point and keeps every compaction boundary it crosses, so a fork always carries its own history. `--partial` (`full=False`) gives the old behaviour, only what the model saw after the last compaction.

For sessions that already have the second shape, the shared uuids identify the parent and the fork point. `export_full` (the CLI `export` default, `--this-file-only` turns it off) prepends the parent's earlier history to the export. `inline_ancestors` (CLI `inline`) writes that earlier history into the child's own file: it is inserted before the first chain line, every original byte and the modification time stay as they were, the new file is re-indexed and checked against the old leaf before an atomic replace, and an open session is refused because Claude Code holds an append handle on its file. After that the parent can be deleted. The inserted lines sit before a compaction boundary whose `parentUuid` is null, which is where Claude Code stops walking when it resumes.

## Web UI

```
convo-chain serve --root ~/.claude/projects
```

Opens a dependency-free local server (default `127.0.0.1:8765`) with a project and session list, the turn by turn conversation, Markdown export (download or save on the server), rename, fork, a recycle bin with restore, and a marker for sessions that a running Claude Code still has open (green when recently active, amber with the idle time when the window is open but quiet). Deleting a session moves it to a recycle-bin project; if another session's earlier history lives only in it, that history is written into the other session first, and the deletion is refused while that other session is open.

It has no login. It binds loopback only unless `--allow-remote` is given, accepts only loopback `Host` headers plus any `--allowed-host`, takes writes only as `application/json` with a matching `Origin`, and writes exports with mode 0600. Put an authenticating reverse proxy in front of it if you expose it. `--sessions-dir` points at Claude Code's live session registry (default `~/.claude/sessions`), `--export-dir` at the export folder (default `~/convo-chain-exports`, strftime codes such as `%Y-%m` allowed), and `--log-header` names a request header whose value is logged next to write operations.

## Where the data lives

Transcripts stay under the caller-provided root, outside this repository. A fork is one new `<uuid>.jsonl` in the source transcript's own project directory. An export goes where `--out` says, or back to the caller in memory. `.gitignore` excludes `*.jsonl` and `*.jsonl.gz` everywhere. `.dataclass.json` records how that was checked.

## Session operations and recovery

Rename appends a native custom-title record and updates the native index. Move preserves transcript
bytes and IDs, carries the whole sidecar tree, and updates both project indexes. The target must be an existing project directory under the same root. It refuses active
writers, destination collisions, links and cross-volume moves. Journals under the caller root's
`.convo-chain-ops` directory allow `recover_pending(root)` to undo interrupted edits before the next
mutation. Concurrent writers must be closed before moving a session; the root lock coordinates
library clients, and file reservations plus before/after checks detect outside interference. An
optional UUID `request_id` on `fork` lets a caller retry a lost response without creating another
session. Project metadata determines the resume directory without rewriting historical cwd.

`delete_plan(sid, root=..., expected_project=...)` reports the file count, bytes and native index
entries covered by a deletion. After confirming that scope, call `delete` with the same session,
project, plan `fingerprint` and a UUID `request_id`. It permanently removes the transcript and
its sidecar tree, retaining the project directory and other sessions. A changed preview is refused;
request retries reuse a receipt and cannot delete a recreated session. Interrupted operations roll
back before commit and resume file cleanup after commit. `cleanup_pending` means some committed
files still need cleanup; retry the same request. These two operations are library APIs only.

## Limitations

The index is per file. Sidecar files of an ancestor (large tool results stored next to its transcript) are not copied when its history is inlined or forked. Resume commands use explicit project metadata or a recorded directory that
matches the storage folder. Missing metadata is reported. Windows file transactions use Windows
10 or newer rename semantics; other platforms need an atomic rename-without-replacement primitive.

## Languages

This README exists in [English](README.md) and [中文](README_CN.md), section for section. Code comments and the messages the library returns are in Chinese.

## Roadmap · Contributing · License

See [ROADMAP.md](ROADMAP.md) and [CHANGELOG.md](CHANGELOG.md). Issues and pull requests are welcome; every example in them must be synthetic. MIT licensed, see [LICENSE](LICENSE).
