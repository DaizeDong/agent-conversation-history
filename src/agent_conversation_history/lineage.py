"""分叉会话的祖先:找父会话、导出时接上前史、把前史写进子文件。

Claude Code 的分叉会话有两种落盘形式:
  - 整段复制:子文件自带全部历史,什么都不用做;
  - 分叉后又压缩(或者 /branch 只带压缩点之后的部分):子文件开头就是 compact_boundary,
    压缩之前的历史只在父会话的文件里。父会话一删,这半段就没了。

这里的做法:用两份文件共有的 uuid 找到父会话和分叉点,父会话在分叉点之前的那一段
就是子会话缺的前史。export_full 导出时把它接在前面;inline_ancestors 把它写进子文件,
之后子会话自带全部历史,父会话可以随便删。
"""
from __future__ import annotations

import json
import mmap
import os
from contextlib import ExitStack
from pathlib import Path

from . import core
from . import transactions as T
from .errors import ConversationHistoryError, Unavailable

MAX_DEPTH = 12           # 祖先最多往上找这么多层
HEAD_RECORDS = 80        # 判断「这个文件是不是从压缩边界开头」时看的行数
SCAN_ENTRIES = 400       # 子会话开头这么多条链条目的 uuid 用来找共有节点
PROBES = 10              # 找父会话时拿子文件开头这么多个 uuid 当探针

_PARENT: dict = {}       # 绝对路径 -> 父会话 id 或 None。文件开头不会变,开着的会话一直在长,所以不能按大小当键


def _head(path: Path, limit: int = HEAD_RECORDS):
    with path.open("rb") as fh:
        for n, line in enumerate(fh):
            if n >= limit:
                return
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                yield rec


def _first_chain_record(path: Path):
    for rec in _head(path):
        if rec.get("uuid") and rec.get("type") in ("user", "assistant", "system", "attachment"):
            return rec
    return None


def _starts_at_boundary(path: Path) -> dict | None:
    rec = _first_chain_record(path)
    if rec and rec.get("subtype") == "compact_boundary" and rec.get("parentUuid") is None:
        return rec
    return None


def _display_order(ix) -> list:
    return core._path(ix, core._leaf_k(ix, None)[0])[0]


def first_shared_pos(child_ix, parent_ix) -> int | None:
    """子会话的开头那些条目,在父会话显示链上最靠前的位置。没有共有节点返回 None。"""
    order = _display_order(parent_ix)
    pos = {parent_ix.E[k]["u"]: n for n, k in enumerate(order)}
    shared = [pos[e["u"]] for e in child_ix.E[:SCAN_ENTRIES] if e["u"] in pos]
    return min(shared) if shared else None


def _files_containing(root: str, needles: list, skip: Path) -> list:
    """root 下哪些会话文件里出现了这些 uuid 里的任何一个。用内存映射找字节串,不逐行解析。"""
    out = []
    pats = [n.encode() for n in needles]
    for f in Path(root).glob("*/*.jsonl"):
        if f == skip:
            continue
        try:
            with f.open("rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                if any(mm.find(p) != -1 for p in pats):
                    out.append(f)
        except (OSError, ValueError):       # 空文件 mmap 会抛 ValueError
            continue
    return out


def parent_of(sid: str, root: str) -> str | None:
    """sid 的父会话 id,不是从压缩边界开头的会话返回 None。

    优先读 forkedFrom;没有的话(会话在压缩时被 Claude Code 自己续写成新文件),用开头那个
    压缩边界的 logicalParentUuid 和开头几个 uuid 去别的文件里找,同样找得到的候选里
    取共有位置最靠后(= 前史最长)的那个:兄弟分叉也会共有尾部,不能取第一个。"""
    loc, ix, _c = core._load(sid, None, root)
    path = loc["main"]
    key = str(path)
    if key in _PARENT:
        return _PARENT[key]
    boundary = _starts_at_boundary(path)
    found = None
    if boundary is not None:
        for rec in _head(path):
            ff = rec.get("forkedFrom")
            if isinstance(ff, dict) and ff.get("sessionId") and ff["sessionId"] != sid:
                found = ff["sessionId"]
                break
        if found is None:
            lp = boundary.get("logicalParentUuid")
            needles = list(dict.fromkeys(([lp] if lp else []) + [e["u"] for e in ix.E[1:PROBES]]))
            best = -1
            for f in _files_containing(root, needles, path):
                try:
                    pix = core._load(f.stem, None, root)[1]
                except (ConversationHistoryError, Unavailable, OSError):
                    continue
                p = first_shared_pos(ix, pix)
                if p is not None and p > best:
                    found, best = f.stem, p
    _PARENT[key] = found
    return found


def missing_ancestors(sid: str, root: str) -> dict | None:
    """sid 缺前史就返回 {parent, cut, order};自带全部历史(或找不到父会话)返回 None。
    order 是父会话显示链上要补的那一段(分叉点之前)的节点序号,cut 是它的条数。"""
    try:
        par = parent_of(sid, root)
        if not par:
            return None
        _l, ix, _c = core._load(sid, None, root)
        _l2, pix, _c2 = core._load(par, None, root)
    except (ConversationHistoryError, Unavailable, OSError):
        return None
    p = first_shared_pos(ix, pix)
    if not p:
        return None
    return {"parent": par, "cut": p, "order": _display_order(pix)[:p]}


def lineage(sid: str, root: str) -> list:
    """[{sid, order}...],从子到祖先:每一层是那个会话自己文件里缺的前史。"""
    out, seen, cur = [], {sid}, sid
    for _ in range(MAX_DEPTH):
        m = missing_ancestors(cur, root)
        if not m or m["parent"] in seen:
            break
        out.append({"sid": m["parent"], "order": m["order"]})
        seen.add(m["parent"])
        cur = m["parent"]
    return out


def dependents_of_all(root: str) -> list:
    """root 下所有缺前史的会话 id。"""
    out = []
    for f in Path(root).glob("*/*.jsonl"):
        try:
            if _starts_at_boundary(f) is not None and missing_ancestors(f.stem, root):
                out.append(f.stem)
        except OSError:
            continue
    return out


def dependents(sid: str, root: str) -> list:
    """哪些会话的前史只在 sid 这个文件里(删掉 sid 它们就会丢历史)。"""
    out = []
    for f in Path(root).glob("*/*.jsonl"):
        if f.stem == sid:
            continue
        try:
            if _starts_at_boundary(f) is None:
                continue
            m = missing_ancestors(f.stem, root)
        except OSError:
            continue
        if m and m["parent"] == sid:
            out.append(f.stem)
    return out


# ---------------------------------------------------------------- 导出

HEADER_END = "- 时间: 本机时区"


def export_full(sid: str, *, to: str, frm: str | None = None, include_tools: bool = False,
                include_thinking: bool = False, root: str) -> dict:
    """export_md,外加把缺的前史接在前面。指定了起点 frm 就只导那一段,不接前史。"""
    main = core.export_md(sid, to=to, frm=frm, include_tools=include_tools,
                          include_thinking=include_thinking, root=root)
    if frm:
        return main
    layers = lineage(sid, root)
    if not layers:
        return main
    parts = []
    for layer in layers:
        pix = core._load(layer["sid"], None, root)[1]
        end = pix.E[layer["order"][-1]]["u"]
        parts.append((layer["sid"], core.export_md(
            layer["sid"], to=end, include_tools=include_tools,
            include_thinking=include_thinking, root=root)))
    head, _, rest = main["text"].partition(HEADER_END)
    out = [head.rstrip("\n"), HEADER_END]
    out += [f"- 前史: `{p}`" for p, _r in reversed(parts)] + [""]
    for p, r in reversed(parts):
        body = r["text"].partition(HEADER_END)[2].lstrip("\n")
        out += [f"## 前史:父会话 `{p[:8]}` 分叉之前的内容", "", body.rstrip(), ""]
    out += [f"## 以下是本会话 `{sid[:8]}` 自己的内容", "", rest.lstrip("\n")]
    res = dict(main)
    res["text"] = "\n".join(out).rstrip() + "\n"
    res["nodes"] += sum(r["nodes"] for _p, r in parts)
    res["turns"] += sum(r["turns"] for _p, r in parts)
    res["truncated"] = main["truncated"] or any(r["truncated"] for _p, r in parts)
    res["ancestors"] = [p for p, _r in parts]
    return res


# ---------------------------------------------------------------- 写进子文件

def _prefix_lines(sid: str, root: str, new_sid: str) -> list:
    """sid 缺的全部前史,按从老到新排好的 JSON 行(已把 sessionId 换成 new_sid)。"""
    layers = lineage(sid, root)
    lines, prev = [], None
    for layer in reversed(layers):                      # 最老的祖先先写
        pix = core._load(layer["sid"], None, root)[1]
        order = layer["order"]
        raws = core._read_lines(pix, order)
        included = {pix.E[k]["u"] for k in order}
        for k in order:
            o = core._obj(raws[k], pix.E[k]["u"])
            o["sessionId"] = new_sid
            if "session_id" in o:
                o["session_id"] = new_sid
            o["isSidechain"] = False
            o.pop("sessionKind", None)
            if pix.E[k]["kind"] == "compact":
                o["parentUuid"] = None
            elif o.get("parentUuid") not in included and not (
                    o.get("parentUuid") is None and prev is None):
                o["parentUuid"] = prev                    # 悬空的父节点接到上一行
            o["forkedFrom"] = {"sessionId": layer["sid"], "messageUuid": pix.E[k]["u"]}
            lines.append(core._dump(o))
            prev = pix.E[k]["u"]
    return lines


def inline_ancestors(sid: str, *, root: str, is_live=None) -> dict:
    """把缺的前史写进 sid 自己的文件,写完它自带全部历史。

    只在文件开头第一条链条目之前插入,原来的每个字节原样保留,所以随时可以反向剥掉。
    先写临时文件并整体重新索引、核对叶子没变,再原子替换;替换前核对文件没被别人写过。
    is_live(sid) 为真就拒绝:开着的 Claude Code 持有这个文件的追加句柄,替换文件会让
    它之后写的东西落进一个已经被顶掉的旧文件。"""
    core.shape(sid)
    if is_live is not None and is_live(sid):
        raise ConversationHistoryError("这个会话正开着,先关掉窗口再补全前史", "session_live")
    loc, ix, _c = core._load(sid, None, root)
    path = loc["main"]
    if loc["gz"]:
        raise ConversationHistoryError("压缩存放的会话文件不改写", "gz")
    base = core._base(root).resolve()
    if core._enclosing_worktree(base) is not None:
        raise ConversationHistoryError("会话修改不能写入未验证的数据仓库,请使用仓库外的会话根目录", "inside_repo")
    m = missing_ancestors(sid, root)
    if m is None:
        return {"id": sid, "inlined": 0, "unchanged": True}
    ancestors = [x["sid"] for x in lineage(sid, root)]
    prefix = _prefix_lines(sid, root, sid)
    if not prefix:
        return {"id": sid, "inlined": 0, "unchanged": True}
    old_leaf = ix.E[core._leaf_k(ix, None)[0]]["u"]
    st = path.stat()
    insert_at = ix.E[0]["off"]
    with path.open("rb") as fh:
        data = fh.read()
    if len(data) != st.st_size:
        raise ConversationHistoryError("文件在读取期间被写入,请稍后重试", "busy")
    blob = ("\n".join(prefix) + "\n").encode("utf-8")
    tmp = path.parent / f".inline-{sid}.tmp"
    with ExitStack() as stack:
        stack.enter_context(T.reserve(path))
        with open(tmp, "xb") as out:
            try:
                out.write(data[:insert_at] + blob + data[insert_at:])
                out.flush()
                os.fsync(out.fileno())
            except BaseException:
                out.close()
                tmp.unlink(missing_ok=True)
                raise
        try:
            nix = core._build(tmp, False)
            new_leaf = nix.E[core._leaf_k(nix, None)[0]]["u"]
            if (new_leaf != old_leaf or nix.bad > ix.bad or nix.dupUuids > ix.dupUuids
                    or len(nix.E) != len(ix.E) + len(prefix)):
                raise ConversationHistoryError("补全后的文件没通过核对,没有替换原文件", "verify_failed")
            now = path.stat()
            if (now.st_size, now.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
                raise ConversationHistoryError("文件在补全期间被写入,没有替换", "busy")
            os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))     # 列表按修改时间排,别把它顶到最前
            os.chmod(tmp, st.st_mode & 0o7777)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
    core.clear_cache()
    _PARENT.clear()
    return {"id": sid, "inlined": len(prefix), "ancestors": ancestors, "unchanged": False}
