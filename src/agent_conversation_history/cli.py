"""The `agent-conversation-history` command line: the library's four operations, JSON on stdout.

    agent-conversation-history chain  SID [--leaf U] [--sub AGENT]
    agent-conversation-history node   SID UUID [--sub AGENT]
    agent-conversation-history export SID --to U [--from U] [--leaf U] [--sub AGENT] [--tools] [--thinking]
                           [--out FILE]
    agent-conversation-history fork   SID AT [--leaf U] [--id UUID] [--title-suffix S] [--partial]
                       (AT: a uuid, leaf, last-human, before-last-human or before:UUID)
    agent-conversation-history inline [SID | --all] [--sessions-dir DIR]
    agent-conversation-history serve  [--host H] [--port N] [--sessions-dir DIR] [--export-dir DIR]

Every subcommand takes `--root DIR` (the directory holding one folder per project, each with
`<session-id>.jsonl` files). Without it the CLI falls back to the `AGENT_CONVERSATION_HISTORY_ROOT` environment
variable. That fallback lives HERE and only here: the library itself never reads the environment,
so a caller that embeds it decides where the root comes from.

Exit codes are part of the contract, so a script can branch without parsing prose:

    0  success, the result object on stdout
    1  refused: {"error": {"code": ..., "message": ...}} on stdout, `code` is stable
    2  usage error (argparse), message on stderr
    3  not checked: no usable root. chain/node print their {"available": false, ...} object,
       export/fork print the refusal with code "unavailable"
    4  internal failure: {"error": {"code": "internal", "message": <exception class name>}} on
       stdout. Only the class name is printed, never the exception text, because that text can
       carry transcript content or paths. This is a bug or an unreadable file, not a refusal.

This CLI is for standalone and agent use. It builds the index from scratch on every call, because
the cache lives in the process; a long-lived caller should import the library instead.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .core import _enclosing_worktree, chain, export_md, fork, node, resolve_node
from .lineage import export_full
from .errors import ConversationHistoryError

ENV_ROOT = "AGENT_CONVERSATION_HISTORY_ROOT"


def _default_root():
    """没给 --root 也没设环境变量时的根目录:$CLAUDE_CONFIG_DIR/projects,否则 ~/.claude/projects。
    库本身不猜目录,这个默认只在命令行里。目录不存在就返回 None,走「未检查」。"""
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    d = Path(cfg) / "projects" if cfg else Path.home() / ".claude" / "projects"
    return str(d) if d.is_dir() else None

EXIT_OK, EXIT_REFUSED, EXIT_USAGE, EXIT_UNAVAILABLE, EXIT_INTERNAL = 0, 1, 2, 3, 4


def _emit(obj) -> None:
    data = json.dumps(obj, ensure_ascii=False, indent=2, default=str) + "\n"
    out = getattr(sys.stdout, "buffer", None)
    if out is not None:
        sys.stdout.flush()
        out.write(data.encode("utf-8"))
        out.flush()
    else:
        sys.stdout.write(data)


def _refused(e: ConversationHistoryError) -> int:
    _emit({"error": {"code": e.code, "message": str(e)}})
    return EXIT_UNAVAILABLE if e.code == "unavailable" else EXIT_REFUSED


def _write_out(out: str, text: str) -> str:
    """Write the Markdown export to `out`. Exclusive create, and never inside a git work tree.

    The export is real conversation text. A file written into a repository's work tree is one
    `git add` away from being published, so the same rule `fork` applies to its own output applies
    here: a target under any directory holding `.git` is refused, not written.
    """
    p = Path(out).expanduser()
    p = p if p.is_absolute() else Path.cwd() / p
    repo = _enclosing_worktree(p.parent)
    if repo is not None:
        raise ConversationHistoryError(f"--out is inside a git work tree ({repo}); an export is real "
                              "conversation text, refusing to write it into any repository",
                              "inside_repo")
    try:
        with open(p, "x", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    except FileExistsError:
        raise ConversationHistoryError(f"--out already exists, refusing to overwrite: {p}", "exists")
    except OSError as e:
        raise ConversationHistoryError(f"could not write --out ({type(e).__name__}): {p}", "write_failed")
    return str(p)


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=None,
                        help=f"session root directory (absolute). Defaults to ${ENV_ROOT}, then "
                             "$CLAUDE_CONFIG_DIR/projects, then ~/.claude/projects.")
    ap = argparse.ArgumentParser(
        prog="agent-conversation-history",
        description="Rebuild, export or fork the conversation a Claude Code transcript held.")
    ap.add_argument("--version", action="version", version=f"agent-conversation-history {__version__}")
    sp = ap.add_subparsers(dest="cmd", required=True)

    c = sp.add_parser("chain", parents=[common], help="the display chain, cut into turns")
    c.add_argument("sid")
    c.add_argument("--leaf")
    c.add_argument("--sub")

    n = sp.add_parser("node", parents=[common], help="one node's full content")
    n.add_argument("sid")
    n.add_argument("uuid")
    n.add_argument("--sub")

    x = sp.add_parser("export", parents=[common], help="Markdown export of a range on the chain")
    x.add_argument("sid")
    x.add_argument("--to", required=True)
    x.add_argument("--from", dest="frm")
    x.add_argument("--leaf")
    x.add_argument("--sub")
    x.add_argument("--tools", action="store_true", help="include tool calls and results")
    x.add_argument("--thinking", action="store_true", help="include thinking blocks")
    x.add_argument("--this-file-only", action="store_true",
                   help="do not prepend the parent session's history before the fork point")
    x.add_argument("--out", help="write the Markdown here (exclusive create) instead of "
                                 "returning it inside the JSON")

    f = sp.add_parser("fork", parents=[common], help="write a new session file forked at AT")
    f.add_argument("sid")
    f.add_argument("at")
    f.add_argument("--leaf")
    f.add_argument("--id", dest="new_id", help="id for the new session; if it already exists the existing result is returned and nothing is written")
    f.add_argument("--title-suffix", help="appended to the original title (default ' (fork @ <first 8 of AT>)'; empty string keeps the title)")
    f.add_argument("--partial", action="store_true",
                   help="copy only what the model saw after the last compaction (old behaviour)")

    i = sp.add_parser("inline", parents=[common],
                      help="write a forked session's missing earlier history into its own file")
    i.add_argument("sid", nargs="?")
    i.add_argument("--all", action="store_true", help="every session that needs it")
    i.add_argument("--sessions-dir", help="Claude Code's live session registry (default ~/.claude/sessions); "
                                          "open sessions are skipped")

    w = sp.add_parser("serve", parents=[common], help="local web UI to browse, export, fork and tidy sessions")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8765)
    w.add_argument("--sessions-dir", help="Claude Code's live session registry (default ~/.claude/sessions)")
    w.add_argument("--export-dir", help="where 'save on server' writes; strftime codes allowed "
                                        "(default ~/agent-conversation-history-exports)")
    w.add_argument("--allowed-host", action="append", default=[],
                   help="extra Host header to accept (repeatable); loopback names are always accepted")
    w.add_argument("--allow-remote", action="store_true",
                   help="bind a non-loopback address; there is no login, put an authenticating proxy in front")
    w.add_argument("--log-header", help="request header whose value is logged for write operations")
    return ap


def _inline(args, root):
    from .lineage import dependents_of_all, inline_ancestors
    from .web import App
    app = App(root or "", args.sessions_dir or str(Path.home() / ".claude" / "sessions"), "")
    live = app.active_sessions()
    if args.all == bool(args.sid):
        raise ConversationHistoryError("give exactly one of SID or --all", "bad_args")
    todo = dependents_of_all(root) if args.all else [args.sid]
    done, skipped = [], []
    for sid in todo:
        try:
            r = inline_ancestors(sid, root=root, is_live=lambda s: s in live)
        except ConversationHistoryError as e:
            if not args.all:
                raise
            skipped.append({"id": sid, "code": e.code})
            continue
        if not r.get("unchanged"):
            done.append(r)
    return {"inlined": done, "skipped": skipped}


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    # An explicit --root, even an empty one, is the caller's answer and is never replaced by the
    # environment: `--root ""` must come out "not checked", not quietly read some other root.
    root = args.root if args.root is not None else (os.environ.get(ENV_ROOT) or _default_root())
    try:
        if args.cmd == "chain":
            res = chain(args.sid, leaf=args.leaf, sub=args.sub, root=root)
        elif args.cmd == "node":
            res = node(args.sid, args.uuid, sub=args.sub, root=root)
        elif args.cmd == "export":
            if args.this_file_only or args.leaf or args.sub:
                res = export_md(args.sid, args.to, frm=args.frm, leaf=args.leaf, sub=args.sub,
                                include_tools=args.tools, include_thinking=args.thinking, root=root)
            else:
                res = export_full(args.sid, to=args.to, frm=args.frm, include_tools=args.tools,
                                  include_thinking=args.thinking, root=root)
            if args.out:
                res = dict(res)
                res["out"] = _write_out(args.out, res.pop("text"))
        elif args.cmd == "inline":
            res = _inline(args, root)
        elif args.cmd == "serve":
            from .web import serve
            return serve(root or "", args.host, args.port,
                         args.sessions_dir, args.export_dir, args.allowed_host,
                         args.allow_remote, args.log_header)
        else:
            at = resolve_node(args.sid, args.at, leaf=args.leaf, root=root)
            res = fork(args.sid, at, leaf=args.leaf, root=root, full=not args.partial,
                       new_id=args.new_id, title_suffix=args.title_suffix)
    except ConversationHistoryError as e:
        return _refused(e)
    except Exception as e:  # noqa: BLE001  the contract promises JSON on stdout for every outcome
        _emit({"error": {"code": "internal", "message": type(e).__name__}})
        return EXIT_INTERNAL
    _emit(res)
    if isinstance(res, dict) and res.get("available") is False:
        return EXIT_UNAVAILABLE
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
