"""HTTP 服务：ThreadingHTTPServer，启动时不扫描不分析，只读库 -> 秒开。
音频用 Range 流；浏览器不支持的格式（ape/wma/wv/tta/dsf）用 ffmpeg 即时转码并缓存。
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import config, library, lyrics as lyr, moods, parse, quality
from .db import DB

HASH = re.compile(r"^[a-f0-9]{6,40}$")

SELECT_SONG = ("SELECT s.id, s.title, s.artist, s.album, s.ext, s.size, s.duration, s.bitrate, "
               "s.samplerate, s.has_meta, s.tempo, s.energy, s.danceability, s.acoustic, s.valence, "
               "s.brightness, s.roughness, s.flatness, s.vocal, s.tonality, s.dynamic, s.mod, s.mood, "
               "s.mood_label, s.mood_reason, s.folder, s.analyzed, "
               "s.q_score, s.q_label, s.q_reason, s.q_kbps, s.q_sr, s.q_cutoff, s.q_fake, s.q_advice, "
               "s.failed, s.path, s.name, (SELECT id FROM lyrics l WHERE l.song_path = s.path) AS lyric_id "
               "FROM songs s")


class Ctx:
    def __init__(self):
        self.cfg = config.load()
        self.db = DB()
        self._ffmpeg = shutil.which(os.environ.get("FFMPEG_BIN", "ffmpeg"))
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def lock_for(self, key: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())

    @property
    def has_ffmpeg(self) -> bool:
        return self._ffmpeg is not None


def _song_out(row: dict, ctx: Ctx) -> dict:
    r = dict(row)
    r["has_cover"] = (config.COVER_DIR / f"{r['id']}.jpg").exists()
    r["dir"] = os.path.dirname(r["path"])
    r["native"] = r["ext"] in config.BROWSER_NATIVE or ctx.has_ffmpeg is False
    return r


# ---------------------------------------------------------------- 路由
def api(ctx: Ctx, method: str, path: str, qs: dict, body: bytes) -> tuple[int, str, bytes, dict]:
    db, cfg = ctx.db, ctx.cfg
    def J(obj, code=200, extra=None):
        return code, "application/json; charset=utf-8", json.dumps(obj, ensure_ascii=False).encode(), (extra or {})

    parts = [p for p in path.split("/") if p]
    head = parts[0] if parts else ""

    # ---------- 状态
    if head == "api" and len(parts) >= 2 and parts[1] == "state":
        counts = db.one("SELECT COUNT(*) total, SUM(analyzed=1) analyzed, SUM(analyzed=0) pending,"
                        " SUM(failed=1) failed FROM songs") or {}
        with library._LOCK:
            st = dict(library.STATE)
        return J({"counts": counts, "state": st, "moods": moods.all_moods(),
                  "scan_roots": cfg["scan_roots"], "version": 1})

    # ---------- 歌单
    if head == "api" and parts[1:2] == ["playlists"]:
        rows = db.q("SELECT p.*, (SELECT COUNT(*) FROM playlist_items pi WHERE pi.playlist_id=p.id) n "
                    "FROM playlists p ORDER BY p.sort, p.id")
        return J(rows)

    # ---------- 歌曲列表
    if head == "api" and parts[1:2] == ["songs"]:
        limit = min(int(qs.get("limit", ["200"])[0]), 3000)
        offset = int(qs.get("offset", ["0"])[0])
        where, args, join = [], [], ""
        pl = qs.get("playlist", [""])[0]
        if pl:
            join = " JOIN playlist_items pi ON pi.song_id=s.id JOIN playlists p ON p.id=pi.playlist_id"
            where.append("p.key=?")
            args.append(pl)
        mood = qs.get("mood", [""])[0]
        if mood:
            where.append("s.mood=?")
            args.append(mood)
        q = qs.get("q", [""])[0].strip()
        if q:
            like = f"%{q}%"
            where.append("(s.title LIKE ? OR s.artist LIKE ? OR s.album LIKE ? OR s.name LIKE ?)")
            args += [like] * 4
        if qs.get("analyzed", [""])[0] == "1":
            where.append("s.analyzed=1")
        wsql = ("WHERE " + " AND ".join(where)) if where else ""
        order = qs.get("order", [""])[0]
        order_sql = {"energy": "s.energy DESC", "tempo": "s.tempo DESC", "title": "s.title COLLATE NOCASE",
                     "duration": "s.duration DESC", "added": "s.added DESC", "random": "RANDOM()",
                     "mood": "s.mood, s.energy DESC"}.get(order, "s.id")
        if pl and not order:
            order_sql = "pi.pos"
        rows = db.q(SELECT_SONG + f"{join} {wsql} ORDER BY {order_sql} LIMIT ? OFFSET ?",
                    args + [limit, offset])
        csql = "SELECT COUNT(*) n FROM songs s" + join + f" {wsql}"
        total = db.one(csql, args)["n"]
        return J({"songs": [_song_out(r, ctx) for r in rows], "total": total})

    # ---------- 单曲
    if head == "api" and parts[1:2] == ["song"] and len(parts) >= 3 and parts[2].isdigit():
        r = db.one(SELECT_SONG + " WHERE s.id=?", (int(parts[2]),))
        if not r:
            return J({"error": "not found"}, 404)
        out = _song_out(r, ctx)
        out["mood_scores"] = {}
        return J(out)

    if head == "api" and parts[1:2] == ["reparse"] and len(parts) >= 3 and parts[2].isdigit():
        sid = int(parts[2])
        r = db.one("SELECT * FROM songs WHERE id=?", (sid,))
        if not r:
            return J({"error": "not found"}, 404)
        info = parse.parse_song(r["path"])
        with db.tx() as c:
            c.execute("UPDATE songs SET title=?,artist=?,album=?,has_meta=?,duration=?,bitrate=?,samplerate=? "
                      "WHERE id=?", (info.title, info.artist, info.album, int(info.has_meta),
                                     info.duration, info.bitrate, info.samplerate, sid))
        # 事务外落盘（save_lyric 自带事务，嵌套会死锁）
        if info.cover:
            library._save_cover(sid, info.cover)
        if info.embedded_lyric:
            library._try_embedded_lyric(db, sid, r["path"], info.title, info.artist,
                                        info.album, info.embedded_lyric, info.duration)
        return J({"ok": True, "title": info.title, "artist": info.artist, "source": info.source})

    # ---------- 歌词
    if head == "api" and parts[1:2] == ["lyric"] and len(parts) >= 3 and parts[2].isdigit():
        sid = int(parts[2])
        if method == "GET":
            data = lyr.load_lyric(db, sid)
            return J(data if data else {"lines": [], "plain": ""})
        if method == "POST":
            payload = json.loads(body or b"{}")
            r = db.one("SELECT * FROM songs WHERE id=?", (sid,))
            if not r:
                return J({"error": "not found"}, 404)
            raw = payload.get("lrc") or payload.get("text") or ""
            lines, meta = lyr.parse_lrc(raw)
            if not lines:
                return J({"error": "歌词为空或无法解析"}, 400)
            pl = lyr.build_payload(r["title"], r["artist"], r["album"], "manual", lines, meta,
                                   r["duration"] or 0)
            lyr.save_lyric(db, sid, r["path"], pl)
            return J({"ok": True, "lines": len(lines)})

    # ---------- 歌词搜索
    if head == "api" and parts[1:2] == ["search"]:
        kw = qs.get("q", [""])[0]
        return J({"results": lyr.search(db, kw, int(qs.get("limit", ["80"])[0]))})

    # ---------- 扫描 / 分析 / 导入
    if head == "api" and method == "POST" and parts[1:2] == ["scan"]:
        t = threading.Thread(target=_scan_then_analyze, args=(ctx,), daemon=True)
        t.start()
        return J({"ok": True, "queued": True})

    if head == "api" and method == "POST" and parts[1:2] == ["analyze"]:
        n = library.analyze_pending(db, cfg)
        return J({"ok": True, "submitted": n})

    if head == "api" and method == "POST" and parts[1:2] == ["reclassify"]:
        # 只改分类规则时用：不重新解码，秒级重排全库
        return J({"ok": True, "reclassified": library.reclassify_only(db, cfg)})

    if head == "api" and method == "POST" and parts[1:2] == ["rebuild"]:
        return J(library.rebuild_playlists(db, cfg))

    if head == "api" and method == "POST" and parts[1:2] == ["import"]:
        payload = json.loads(body or b"{}")
        d = payload.get("dir", "")
        if not d:
            return J({"error": "缺少 dir"}, 400)
        stats = lyr.import_dir(db, d)
        if payload.get("remember") and d not in cfg["lyrics_import_dirs"]:
            cfg["lyrics_import_dirs"].append(d)
            config.save(cfg)
        return J(stats)

    # ---------- 音质评判 / 自动去重（只标记，不删文件）
    if head == "api" and parts[1:2] == ["quality"]:
        act = parts[2] if len(parts) > 2 else ""
        if act == "overview":
            return J(quality.overview(db))
        if act == "list":
            return J(quality.list_rows(db, qs.get("filter", ["upgrade"])[0],
                                       qs.get("q", [""])[0],
                                       int(qs.get("limit", ["200"])[0]),
                                       int(qs.get("offset", ["0"])[0])))
        if act == "duplicates":
            return J(quality.duplicate_groups(db, int(qs.get("limit", ["100"])[0])))
        if act == "state":
            return J(quality.deep_state())
        # 状态自愈：同步「文件已被移走但曲库仍有记录」的历史不一致
        if method == "POST" and act == "sync":
            return J(quality.sync_missing(db))
        # 安全清除：预览 -> 用户确认 -> 携带令牌执行；一律移入回收站，不直接删除
        if act == "trash":
            sub = parts[3] if len(parts) > 3 else ""
            if method == "GET" and sub == "log":
                return J(quality.trash_log(int(qs.get("limit", ["50"])[0])))
            if method == "POST" and sub == "preview":
                payload = json.loads(body or b"{}")
                return J(quality.preview_trash(db, payload.get("ids"), cfg))
            if method == "POST" and not sub:
                payload = json.loads(body or b"{}")
                return J(quality.trash_files(db, payload.get("ids"),
                                             payload.get("token", ""), cfg))
            return J({"error": "no such trash api"}, 404)
        if act == "export":
            kind = qs.get("kind", ["upgrade"])[0]
            if kind not in ("upgrade", "dup", "all"):
                return J({"error": "kind 不合法"}, 400)
            data = quality.export_csv(db, kind)
            return (200, "text/csv; charset=utf-8", data,
                    {"Content-Disposition": f'attachment; filename="music-quality-{kind}.csv"'})
        if method == "POST" and act == "refresh":
            payload = json.loads(body or b"{}")
            return J(quality.refresh(db, bool(payload.get("force"))))
        if method == "POST" and act == "deep":
            payload = json.loads(body or b"{}")
            return J(quality.deep_scan(db, int(payload.get("limit") or 0),
                                       int(payload.get("workers") or 2)))
        return J({"error": "no such quality api"}, 404)

    if head == "api" and parts[1:2] == ["config"]:
        if method == "GET":
            return J(cfg)
        payload = json.loads(body or b"{}")
        for k in ("scan_roots", "exclude_dirs", "exclude_roots", "port", "analyze_seconds",
                  "analyze_workers", "max_playlist_songs", "lyrics_import_dirs", "open_browser"):
            if k in payload:
                cfg[k] = payload[k]
            cfg["scan_roots"] = [p for p in cfg["scan_roots"] if isinstance(p, str)]
        config.save(cfg)
        return J(cfg)

    if head == "api" and method == "POST" and parts[1:2] == ["playlist"]:
        payload = json.loads(body or b"{}")
        name = (payload.get("name") or "").strip()
        if not name:
            return J({"error": "缺少 name"}, 400)
        where = payload.get("where") or "1=1"
        # 只允许安全的比较表达式
        if not re.fullmatch(r"[\w\s=<>!%.,'\"()\-\*/+ANDORNULLIS LIKEa-z0-9_]+", where, re.I):
            return J({"error": "条件表达式不合法"}, 400)
        return J(library.create_smart(db, name, payload.get("emoji", "⭐"), where,
                                      payload.get("order", "energy DESC"),
                                      int(payload.get("limit", 300))))

    return J({"error": "no such api"}, 404)


def _scan_then_analyze(ctx: Ctx) -> None:
    res = library.scan(ctx.db, ctx.cfg)
    if not res.get("skipped"):
        library.rebuild_playlists(ctx.db, ctx.cfg)
        library.analyze_pending(ctx.db, ctx.cfg)
    quality.refresh(ctx.db)              # 新入库歌曲也要立刻有音质评分与去重结果


# ---------------------------------------------------------------- Handler
class Handler(BaseHTTPRequestHandler):
    server_version = "MoodPlayer/1.0"
    protocol_version = "HTTP/1.1"
    ctx: Ctx = None  # type: ignore

    def log_message(self, fmt, *args):  # 静音访问日志
        pass

    def _send(self, code: int, ctype: str, data: bytes, extra: dict | None = None,
              content_range: str | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if content_range:
            self.send_header("Content-Range", content_range)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _send_file(self, fp: Path, ctype: str | None = None):
        """Range 流式发送。"""
        try:
            size = fp.stat().st_size
        except OSError:
            return self._send(404, "text/plain", b"missing")
        ctype = ctype or mimetypes.guess_type(str(fp))[0] or "application/octet-stream"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        code = 200
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2) or size - 1)
                elif m.group(2):
                    start = max(0, size - int(m.group(2)))
                code = 206
                end = min(end, size - 1)
                if start > end:
                    return self._send(416, "text/plain", b"bad range",
                                      {"Content-Range": f"bytes */{size}"})
        with open(fp, "rb") as f:
            f.seek(start)
            length = end - start + 1
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            if code == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return
            remaining = length
            try:
                while remaining > 0:
                    chunk = f.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    # 统一入口
    def do_GET(self):  self._dispatch("GET")
    def do_HEAD(self): self._dispatch("HEAD")
    def do_POST(self): self._dispatch("POST")

    def _dispatch(self, method: str):
        url = urlparse(self.path)
        path = unquote(url.path)
        qs = parse_qs(url.query)
        body = b""
        if method == "POST":
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b""
        try:
            parts = [p for p in path.split("/") if p]
            if parts and parts[0] == "api":
                code, ctype, data, extra = api(self.ctx, method, path, qs, body)
                return self._send(code, ctype, data, extra)
            if parts and parts[0] == "audio" and len(parts) == 2 and parts[1].isdigit():
                return self._serve_audio(int(parts[1]))
            if parts and parts[0] == "cover" and len(parts) == 2 and parts[1].isdigit():
                fp = config.COVER_DIR / f"{parts[1]}.jpg"
                if fp.exists():
                    return self._send_file(fp, "image/jpeg")
                return self._send(404, "image/png", b"")
            # 静态资源
            rel = path.lstrip("/") or "index.html"
            fp = (config.WEB_DIR / rel).resolve()
            if str(fp).startswith(str(config.WEB_DIR.resolve())) and fp.is_file():
                return self._send_file(fp)
            return self._send(404, "text/plain; charset=utf-8", "404".encode())
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self._send(500, "application/json",
                           json.dumps({"error": f"{type(e).__name__}: {e}"}).encode())
            except Exception:
                pass

    def _serve_audio(self, song_id: int):
        ctx = self.ctx
        r = ctx.db.one("SELECT path, ext FROM songs WHERE id=?", (song_id,))
        if not r:
            return self._send(404, "text/plain", b"no song")
        src = Path(r["path"])
        if not src.is_file():
            return self._send(404, "text/plain", b"file gone")
        ext = r["ext"].lower()
        if ext in config.BROWSER_NATIVE or not ctx.has_ffmpeg:
            return self._send_file(src)
        # 转码缓存（文件名用稳定哈希，跨重启复用）
        import hashlib
        tag = hashlib.md5(f"{src}|{r['ext']}|{src.stat().st_size}".encode("utf-8")).hexdigest()[:16]
        cache = config.CACHE_DIR / f"{tag}.mp3"
        if not cache.exists():
            with ctx.lock_for(str(src)):
                if not cache.exists():
                    tmp = cache.with_suffix(".part")
                    cmd = [ctx._ffmpeg, "-v", "error", "-y", "-i", str(src),
                           "-vn", "-acodec", "libmp3lame", "-q:a", "2", "-ar", "44100",
                           "-f", "mp3", str(tmp)]      # .part 后缀 ffmpeg 猜不出格式，显式 -f
                    try:
                        subprocess.run(cmd, capture_output=True, timeout=300, check=True)
                        tmp.rename(cache)
                    except Exception as e:
                        tmp.unlink(missing_ok=True)
                        return self._send(500, "text/plain", f"transcode failed: {e}".encode())
        return self._send_file(cache)


class QuietServer(ThreadingHTTPServer):
    """浏览器切歌/取消加载会掐断 keep-alive 连接，这类断连不打堆栈。"""
    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, TimeoutError)) or \
                getattr(exc, "errno", None) in (32, 10053, 10054):
            return
        super().handle_error(request, client_address)


def serve(ctx: Ctx | None = None) -> tuple[ThreadingHTTPServer, Ctx]:
    ctx = ctx or Ctx()
    Handler.ctx = ctx
    srv = QuietServer((ctx.cfg["host"], ctx.cfg["port"]), Handler)
    return srv, ctx
