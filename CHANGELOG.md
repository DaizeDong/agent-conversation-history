# Changelog

All notable changes to this project are documented here (Keep a Changelog style).

## [Unreleased]

### Added

- **The package is now `agent-conversation-history`** (import `agent_conversation_history`, command `agent-conversation-history`, environment variable `AGENT_CONVERSATION_HISTORY_ROOT`, error class `ConversationHistoryError`, journal folder `.agent-conversation-history-ops`), and Python 3.10 is enough. The old names are not kept.
- **Fork points for programs.** `resolve_node` and the CLI accept `leaf`, `last-human`, `before-last-human` and `before:UUID` as `AT`; `fork` takes `new_id` (`--id`, idempotent, an existing session is returned as `reused`) and `title_suffix` (`--title-suffix`) and returns `at` and `forkedFrom`. The CLI defaults its root to `$CLAUDE_CONFIG_DIR/projects`, then `~/.claude/projects`.
- **`agent-conversation-history serve`.** A dependency-free local web UI (standard library only) to list projects and sessions, read a conversation turn by turn, export, rename, fork, and move sessions to a recycle bin and back. Open sessions are marked from Claude Code's live registry, green when recently active and amber with the idle time otherwise; the idle time comes from the last real user or assistant line, not the file modification time. Loopback only by default, Host allowlist, JSON-only writes with an Origin check, exports written 0600.
- **`export_full` and `inline_ancestors`, CLI `inline`.** A session that starts at a compaction boundary keeps the history before it only in its parent's file. `export_full` prepends that history to an export, `inline_ancestors` writes it into the child's own file (insert before the first chain line, atomic replace after re-indexing and a leaf check, refused while the session is open), and `dependents` lists the sessions that would lose history if a given session were deleted. The web UI's delete inlines dependents first and is refused while one of them is open.

### Changed

- **`fork` copies the whole history by default.** The display chain up to the fork point is written with every compaction boundary kept (a boundary's `parentUuid` stays null), so a fork is self-contained and its parent can be deleted. `full=False` / `--partial` restores the old behaviour of starting at the last compaction. `export` on the CLI now prepends the parent's earlier history by default; `--this-file-only` turns that off.

- **The guards and style submodules, the gate workflows and fleet sync are gone.** `.githooks` now forwards to the local `fleet-guards` and `fleet-style` checkouts named by `git config fleet.guards` and `fleet.style` and blocks when either is unset or missing; the commit hook also runs the dash check on added lines. `pii-guard.yml`, `dash-guard.yml` and `fleet-sync.yml` are deleted, and `FLEET_SYNC_TOKEN` is removed.
- **The test suite is deleted.** Per the owner rule that a confirmed-working feature keeps no tests, `tests/`, `tools/make_fixtures.py`, the pytest config and the `tests` workflow are gone; CI no longer runs tests.
- **The transcript probes are calibrated.** fleet-guards 2f1edb1 taught the shared data boundary guard the Claude Code transcript shapes, so the `guards/` pin moved to it and the five transcript probes left `_run_shape_probes_pending` for `_run_shape_probes`; `data_boundary.py --calibration` now reports this repository calibrated. The exported Markdown name stays pending, because no shape separates it from documentation. The `.gitignore` rules for transcripts remain as defence in depth.
- **Enrolled in fleet sync.** The repository now has its `FLEET_SYNC_TOKEN` and is listed in both kits' subscriber lists, so `fleet-sync.yml` is back to the shared template, daily `schedule:` included, and the `guards/` and `style/` pins advance through the verified sync workflow instead of by hand.

### Fixed

- **Moving or deleting a session with a sidecar directory failed with `busy` on Linux.** After renaming the tree into place, `move` and `delete` re-took an exclusive `flock` on files they already held locked through the same transaction, which always failed (`BlockingIOError`, reported as the session being in use). They now compare a plain snapshot, since the reservation is already held. Every session with a non-empty sidecar directory (`subagents`, `tool-results`, `custom-title.json`) was affected.

## [0.3.0]

The current package and `__version__` declare 0.3.0. No release date is recorded here; the implementation commits do not establish a publication date.

### Added

- Preview-bound permanent session deletion, including sidecars and native index entries, with interrupted-operation recovery and request replay receipts.
- Transactional rename and move, project metadata for resume directories, and optional request identity for retrying a fork. These capabilities were added after the documented 0.1.0 baseline.

## [0.1.0] - 2026-09-27

### Added

- **Initial extraction from task-console.** The conversation chain engine (`convtree.py`, written on an unpublished task-console feature branch for its conversation chain panel) moved here as the `agent_conversation_history` package, behaviour unchanged: byte offset index with an in-process LRU cache, the shape gate, `locate`, `chain`, `node`, `export_md`, `fork` with exclusive create, and the resume command.
- **Its own error types.** `ConversationHistoryError` (with a stable `.code`) replaces task-console's `maint.Refused`, and `Unavailable` stays a separate class so "not checked" can never be caught as a refusal.
- **The transcript rules, in one place.** `typed_text` and `looks_injected` moved here from task-console's `convos.py`, so the console and this library share one implementation instead of two.
- **A CLI**, `agent-conversation-history` (and `python -m agent_conversation_history`): `chain`, `node`, `export` (with `--out` for a Markdown file) and `fork`, JSON on stdout for every outcome, stable exit codes (`4` with code `internal` when something other than a refusal goes wrong, naming only the exception class). An explicit `--root`, even an empty one, is never replaced by `AGENT_CONVERSATION_HISTORY_ROOT`.
- Repository scaffolding: the `guards/` and `style/` submodules, `.githooks` forwarders, the pii-guard, dash-guard, tests and fleet-sync workflows, `.dataclass.json` and `.pii-allow`.

### Changed

- **The root is always passed in.** The engine used to fall back to the `TASK_CONSOLE_SESSIONS` environment variable when no root was given. The library now reads no environment variable at all; only the CLI reads `AGENT_CONVERSATION_HISTORY_ROOT`, and only as the default for `--root`.

### Fixed

- **A user line whose `message` is not an object no longer breaks indexing.** `typed_text` used to call `.get` on it and raise, which took the whole session down; it now answers "not typed text" and the line is indexed as a meta line.
