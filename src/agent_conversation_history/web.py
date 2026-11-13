"""agent-conversation-history serve:会话浏览、导出、改名、分叉、回收站的本地网页界面,只用标准库。

会话原文里常有凭据和内部细节,所以:
  - 默认只绑回环地址;要绑别的地址必须显式 --allow-remote,而且这里没有登录,
    要放在你自己的认证反向代理后面;
  - Host 头必须在白名单里(回环名 + --allowed-host),挡 DNS 重绑定;
  - 写操作只收 application/json,带 Origin 时它必须和 Host 一致,挡跨站表单;
  - 导出落盘一律 0600,目录 0700;
  - 删除是软删除:整个会话挪进回收站项目目录,原位置记在清单里,随时能恢复;
  - 任何写操作都先挡掉 Claude Code 正开着的会话。
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from . import __version__, core, lineage
from .errors import ConversationHistoryError, Unavailable
from .session_ops import move, rename
from .transcript import looks_injected, typed_text

TRASH_PROJECT = "trash-convo-console"
STATIC = Path(__file__).parent / "web_static"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}

log = logging.getLogger("agent-conversation-history.web")


class Refused(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class App:
    """一个服务实例的配置和全部业务逻辑,和 HTTP 层分开。"""

    def __init__(self, root: str, sessions_dir: str, export_dir: str):
        self.root = Path(root)
        self.sessions_dir = Path(sessions_dir)
        self.export_dir = export_dir

    # ------------------------------------------------------------ 活跃会话

    def active_sessions(self) -> dict:
        """Claude Code 正开着的会话 -> 它的名字。进程还在才算,残留的登记文件不算。"""
        out: dict = {}
        if not self.sessions_dir.is_dir():
            return out
        for f in self.sessions_dir.glob("*.json"):
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
                sid, pid = rec.get("sessionId"), int(rec.get("pid"))
            except (OSError, ValueError, TypeError):
                continue
            if not sid or not _pid_is_claude(pid):
                continue
            out[sid] = rec.get("name") or f"pid {pid}"
        return out

    def guard_inactive(self, sid: str) -> None:
        live = self.active_sessions()
        if sid in live:
            raise Refused(409, "session_live", f"这个会话正开着({live[sid]}),先关掉再操作")

    # ------------------------------------------------------------ 回收站

    def trash_dir(self) -> Path:
        d = self.root / TRASH_PROJECT
        if not d.is_dir():
            d.mkdir(parents=True)
            os.chmod(d, 0o700)
        return d

    def trash_manifest(self) -> dict:
        path = self.trash_dir() / "origins.json"
        try:
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, ValueError):
            return {}

    def write_manifest(self, data: dict) -> None:
        path = self.trash_dir() / "origins.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(path)

    # ------------------------------------------------------------ 列表

    def session_rows(self, project: Path) -> list:
        live = self.active_sessions()
        rows = []
        for f in project.glob("*.jsonl"):
            try:
                st = f.stat()
            except OSError:
                continue
            title, renamed = quick_title(f)
            rows.append({
                "id": f.stem, "project": project.name, "title": title, "renamed": renamed,
                "bytes": st.st_size, "mtime": st.st_mtime, "live": f.stem in live,
                "lastActive": last_activity(f) if f.stem in live else None,
            })
        rows.sort(key=lambda r: r["mtime"], reverse=True)
        return rows

    def projects(self) -> dict:
        out = []
        for d in sorted(p for p in self.root.iterdir() if p.is_dir()):
            if d.name == TRASH_PROJECT:
                continue
            stats = []
            for f in d.glob("*.jsonl"):
                try:
                    stats.append(f.stat())
                except OSError:
                    pass
            if stats:
                out.append({"name": d.name, "sessions": len(stats),
                            "bytes": sum(s.st_size for s in stats),
                            "mtime": max(s.st_mtime for s in stats)})
        out.sort(key=lambda p: p["mtime"], reverse=True)
        return {"root": str(self.root), "projects": out,
                "trashCount": len(list(self.trash_dir().glob("*.jsonl")))}

    def sessions(self, project: str) -> dict:
        d = self.root / project
        if not d.is_dir() or d.resolve().parent != self.root.resolve():
            raise Refused(404, "bad_project", "项目目录不存在")
        return {"project": project, "sessions": self.session_rows(d)}

    # ------------------------------------------------------------ 读

    def chain(self, sid: str, leaf=None, sub=None, steps=False) -> dict:
        result = core.chain(sid, leaf=leaf, sub=sub, root=str(self.root))
        if not steps:
            for turn in result.get("turns", []):
                if "steps" in turn:
                    turn["stepCount"] = len(turn["steps"])
                    turn["steps"] = []
        result["live"] = sid in self.active_sessions()
        if result["live"]:
            result["lastActive"] = last_activity(core.locate(sid, root=str(self.root))["main"])
        if not sub:
            miss = lineage.missing_ancestors(sid, str(self.root))
            if miss:
                result["missingAncestors"] = {"parent": miss["parent"], "nodes": miss["cut"]}
        return result

    def steps(self, sid: str, index: int, leaf=None, sub=None) -> dict:
        result = core.chain(sid, leaf=leaf, sub=sub, root=str(self.root))
        turns = result.get("turns", [])
        if index < 0 or index >= len(turns):
            raise Refused(404, "bad_turn", "没有这一轮")
        return {"steps": turns[index].get("steps", []), "cached": result.get("cached")}

    # ------------------------------------------------------------ 导出

    def export(self, sid: str, to: str, frm=None, tools=False, thinking=False, save=False) -> dict:
        result = lineage.export_full(sid, to=to, frm=frm, include_tools=tools,
                                     include_thinking=thinking, root=str(self.root))
        if save:
            d = Path(time.strftime(self.export_dir))
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o700)
            dest = d / result["filename"]
            dest.write_text(result["text"], encoding="utf-8")
            os.chmod(dest, 0o600)
            result["saved"] = str(dest)
        return result

    # ------------------------------------------------------------ 写

    def rename(self, sid: str, title: str) -> dict:
        self.guard_inactive(sid)
        return rename(sid, title, root=str(self.root))

    def fork(self, sid: str, at: str, leaf=None) -> dict:
        return core.fork(sid, at=at, leaf=leaf, root=str(self.root))

    def inline(self, sid: str) -> dict:
        return lineage.inline_ancestors(sid, root=str(self.root),
                                        is_live=lambda s: s in self.active_sessions())

    def trash(self, sid: str) -> dict:
        """软删除。别的会话的前史如果只在这个文件里,先把前史写进它们自己的文件再挪。"""
        self.guard_inactive(sid)
        root = str(self.root)
        deps = lineage.dependents(sid, root)
        live = self.active_sessions()
        busy = [d for d in deps if d in live]
        if busy:
            names = ", ".join(f"{d[:8]}({live[d]})" for d in busy)
            raise Refused(409, "dependents_live",
                          f"别的会话的前史只在这个会话里,而它们正开着: {names}。先关掉它们再删")
        for d in deps:
            lineage.inline_ancestors(d, root=root, is_live=lambda s: s in live)
        origin = core.locate(sid, root=root)["projectDir"].name
        self.trash_dir()
        result = move(sid, TRASH_PROJECT, root=root)
        manifest = self.trash_manifest()
        manifest[sid] = {"project": origin, "at": time.time()}
        self.write_manifest(manifest)
        result["origin"] = origin
        result["inlinedInto"] = deps
        return result

    def trash_list(self) -> dict:
        manifest = self.trash_manifest()
        rows = self.session_rows(self.trash_dir())
        for r in rows:
            meta = manifest.get(r["id"], {})
            r["origin"], r["trashedAt"] = meta.get("project"), meta.get("at")
        return {"sessions": rows}

    def restore(self, sid: str) -> dict:
        manifest = self.trash_manifest()
        origin = (manifest.get(sid) or {}).get("project")
        if not origin:
            raise Refused(400, "no_origin", "回收站里没有这个会话的原始位置记录")
        if not (self.root / origin).is_dir():
            raise Refused(400, "origin_gone", f"原项目目录 {origin} 已经不在了")
        result = move(sid, origin, root=str(self.root))
        manifest.pop(sid, None)
        self.write_manifest(manifest)
        return result

    def health(self) -> dict:
        return {"ok": True, "root": str(self.root), "version": __version__,
                "live": len(self.active_sessions()),
                "exportDir": time.strftime(self.export_dir)}


# ---------------------------------------------------------------- 工具函数

def _pid_is_claude(pid: int) -> bool:
    """进程还在,并且(能查到命令行时)确实是 claude,避免 pid 被别的进程复用后误报占用。"""
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return True                         # 没有 /proc(macOS、Windows):只认进程存在
    if not cmd.strip(b"\0"):
        return True                         # 正好在 execve 里或是僵尸进程,命令行读成空:宁可当作还开着
    return b"claude" in cmd.lower()


def last_activity(path: Path):
    """最后一条真实对话(user/assistant)的时间戳。开着的 Claude Code 会往文件尾部反复
    追加元数据行,文件修改时间不能当活跃时间用。只读尾部 512KB,找不到返回 None。"""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            fh.seek(max(0, size - 524288))
            tail = fh.read()
    except OSError:
        return None
    for line in reversed(tail.split(b"\n")):
        if b'"timestamp"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("type") in ("user", "assistant") and rec.get("timestamp"):
            try:
                return datetime.fromisoformat(rec["timestamp"].replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
    return None


def quick_title(path: Path):
    """便宜地拿标题:优先 /rename 写的 custom-title,否则第一条用户消息。"""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > 262144:
                fh.seek(-262144, os.SEEK_END)
            tail = fh.read()
        for line in reversed(tail.split(b"\n")):
            if b"custom-title" not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            t = rec.get("customTitle") or rec.get("title")
            if isinstance(t, str) and t.strip():
                return t.strip(), True
    except OSError:
        pass
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for _ in range(60):
                line = fh.readline()
                if not line:
                    break
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("type") != "user":
                    continue
                text = typed_text(rec)
                if text and not looks_injected(text):
                    return " ".join(text.split())[:120], False
    except OSError:
        pass
    return "(无标题)", False


# ---------------------------------------------------------------- HTTP 层

GET_ROUTES = [
    (re.compile(r"^/api/projects$"), lambda a, m, q: a.projects()),
    (re.compile(r"^/api/sessions$"), lambda a, m, q: a.sessions(_one(q, "project"))),
    (re.compile(r"^/api/chain/([^/]+)$"),
     lambda a, m, q: a.chain(m[1], _opt(q, "leaf"), _opt(q, "sub"), _opt(q, "steps") == "true")),
    (re.compile(r"^/api/steps/([^/]+)/(-?\d+)$"),
     lambda a, m, q: a.steps(m[1], int(m[2]), _opt(q, "leaf"), _opt(q, "sub"))),
    (re.compile(r"^/api/node/([^/]+)/([^/]+)$"),
     lambda a, m, q: core.node(m[1], m[2], sub=_opt(q, "sub"), root=str(a.root))),
    (re.compile(r"^/api/trash$"), lambda a, m, q: a.trash_list()),
    (re.compile(r"^/api/health$"), lambda a, m, q: a.health()),
]

POST_ROUTES = [
    (re.compile(r"^/api/export/([^/]+)$"),
     lambda a, m, b: a.export(m[1], b["to"], b.get("frm"), bool(b.get("tools")),
                              bool(b.get("thinking")), bool(b.get("save")))),
    (re.compile(r"^/api/rename/([^/]+)$"), lambda a, m, b: a.rename(m[1], _title(b))),
    (re.compile(r"^/api/fork/([^/]+)$"), lambda a, m, b: a.fork(m[1], b["at"], b.get("leaf"))),
    (re.compile(r"^/api/inline/([^/]+)$"), lambda a, m, b: a.inline(m[1])),
    (re.compile(r"^/api/trash/([^/]+)/restore$"), lambda a, m, b: a.restore(m[1])),
    (re.compile(r"^/api/trash/([^/]+)$"), lambda a, m, b: a.trash(m[1])),
]


def _opt(q: dict, k: str):
    v = q.get(k)
    return v[0] if v else None


def _one(q: dict, k: str) -> str:
    v = _opt(q, k)
    if v is None:
        raise Refused(400, "missing_param", f"缺少参数 {k}")
    return v


def _title(body: dict) -> str:
    t = body.get("title")
    if not isinstance(t, str) or not 1 <= len(t) <= 200:
        raise Refused(400, "bad_title", "标题要 1 到 200 个字符")
    return t


def make_handler(app: App, allowed_hosts: set, log_header: str | None):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"agent-conversation-history/{__version__}"

        def log_message(self, fmt, *args):      # 访问日志不打,写操作在 _finish 里单独记
            pass

        # 公共
        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").strip().lower()
            host = host[1:host.index("]")] if host.startswith("[") else host.rsplit(":", 1)[0]
            return host in allowed_hosts

        def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj):
            self._send(status, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _error(self, status: int, code: str, message: str):
            self._json(status, {"detail": {"code": code, "message": message}})

        def _run(self, fn):
            try:
                return fn()
            except Refused as e:
                self._error(e.status, e.code, e.message)
            except ConversationHistoryError as e:
                self._error(400, e.code, str(e))
            except Unavailable as e:
                self._error(503, "unavailable", str(e))
            except KeyError as e:
                self._error(400, "missing_field", f"缺少字段 {e.args[0]}")
            except Exception as e:  # noqa: BLE001
                log.exception("unhandled")
                self._error(500, "internal", type(e).__name__)
            return None

        # GET
        def do_GET(self):
            if not self._host_ok():
                return self._error(421, "bad_host", "Host 不在白名单里")
            url = urlsplit(self.path)
            path, q = unquote(url.path), parse_qs(url.query)
            if path in ("/", "/index.html"):
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            m = re.match(r"^/api/export/([^/]+)/download$", path)
            if m:
                def dl():
                    r = app.export(m[1], _one(q, "to"), _opt(q, "frm"), _opt(q, "tools") == "true",
                                   _opt(q, "thinking") == "true")
                    self._send(200, r["text"].encode("utf-8"), "text/markdown; charset=utf-8",
                               {"Content-Disposition": f'attachment; filename="{r["filename"]}"'})
                return self._run(dl)
            for rx, fn in GET_ROUTES:
                m = rx.match(path)
                if m:
                    res = self._run(lambda: fn(app, m, q))
                    if res is not None:
                        self._json(200, res)
                    return None
            return self._error(404, "not_found", "没有这个接口")

        # POST
        def do_POST(self):
            if not self._host_ok():
                return self._error(421, "bad_host", "Host 不在白名单里")
            if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
                return self._error(415, "bad_content_type", "写操作只收 application/json")
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc.lower() != (self.headers.get("Host") or "").lower():
                return self._error(403, "bad_origin", "Origin 和 Host 不一致")
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n <= (1 << 20) else None
            except (ValueError, OSError):
                body = None
            if not isinstance(body, dict):
                return self._error(400, "bad_body", "请求体不是 JSON 对象")
            path = unquote(urlsplit(self.path).path)
            for rx, fn in POST_ROUTES:
                m = rx.match(path)
                if m:
                    res = self._run(lambda: fn(app, m, body))
                    who = (self.headers.get(log_header) if log_header else None) or self.client_address[0]
                    log.info("%s POST %s -> %s", who, path, "ok" if res is not None else "refused")
                    if res is not None:
                        self._json(200, res)
                    return None
            return self._error(404, "not_found", "没有这个接口")

    return Handler


def serve(root: str, host: str = "127.0.0.1", port: int = 8765, sessions_dir: str | None = None,
          export_dir: str | None = None, allowed_hosts=(), allow_remote: bool = False,
          log_header: str | None = None) -> int:
    loopback = host in LOOPBACK
    if not loopback and not allow_remote:
        print(f"拒绝绑定 {host}: 这个界面没有登录。要对外就加 --allow-remote,"
              "并放在你自己的认证反向代理后面。", file=sys.stderr)
        return 2
    rootp = Path(root).expanduser()
    if not rootp.is_dir():
        print(f"会话根目录不存在: {rootp}", file=sys.stderr)
        return 3
    app = App(str(rootp),
              str(Path(sessions_dir).expanduser()) if sessions_dir else str(Path.home() / ".claude" / "sessions"),
              str(Path(export_dir).expanduser()) if export_dir else str(Path.home() / "agent-conversation-history-exports"))
    hosts = set(LOOPBACK) | {h.lower() for h in allowed_hosts}
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    srv = ThreadingHTTPServer((host, port), make_handler(app, hosts, log_header))
    srv.daemon_threads = True
    log.info("agent-conversation-history %s serving %s on http://%s:%s", __version__, rootp, host, port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0
