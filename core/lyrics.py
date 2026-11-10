"""歌词：LRC 解析、JSON 存储、全文搜索。
- 每首歌一个 JSON：data/lyrics/<song_id>.json（结构化时间轴，前端直接可用）
- 同时写入 SQLite lyrics 表（plain 字段做 LIKE 搜索，中文无需分词）
- 支持导入外部 .lrc / .json 歌词库目录
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import config
from .db import DB

LRC_TIME = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")
META_KEY = re.compile(r"^\s*\[(ar|ti|al|by|offset|length|la)\]\s*:?\s*(.*)$", re.I)
# 无时间轴的纯文本歌词行分隔
PLAIN_SPLIT = re.compile(r"[\n\r]+|[/|｜]")


def parse_lrc(text: str) -> tuple[list[dict], dict]:
    """解析 LRC -> (lines[{t,text}], meta)。容错：重复时间戳、缺毫秒、无时间轴。"""
    text = text.replace("\ufeff", "")
    meta: dict[str, str] = {}
    stamped: list[dict] = []
    plain: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = META_KEY.match(line)
        if m:
            meta[m.group(1).lower()] = m.group(2).strip()
            continue
        times = LRC_TIME.findall(line)
        if times:
            content = LRC_TIME.sub("", line).strip()
            for mm, ss, frac in times:
                sec = int(mm) * 60 + int(ss)
                if frac:
                    sec += float("0." + frac)
                stamped.append({"t": round(sec, 3), "text": content})
        else:
            plain.append(line)
    if stamped:
        stamped.sort(key=lambda x: x["t"])
        return stamped, meta
    # 没有时间轴：均分（配合歌曲时长），前端可做近似滚动
    lines: list[str] = []
    for p in plain:
        for seg in PLAIN_SPLIT.split(p):
            seg = seg.strip()
            if seg:
                lines.append(seg)
    return [{"t": -1.0, "text": s} for s in lines], meta


def build_payload(title: str, artist: str, album: str, source: str,
                  lines: list[dict], meta: dict, duration: float) -> dict:
    lang = "zh" if re.search(r"[\u4e00-\u9fff]", " ".join(l["text"] for l in lines[:40])) else \
           ("ja" if re.search(r"[\u3040-\u30ff]", " ".join(l["text"] for l in lines[:40])) else "en")
    if meta.get("ar"):
        artist = artist or meta["ar"]
    if meta.get("ti"):
        title = title or meta["ti"]
    if meta.get("al"):
        album = album or meta["al"]
    # 无时间轴时按 duration 均分
    if lines and all(l["t"] < 0 for l in lines) and duration > 0:
        span = duration * 0.92 / max(len(lines), 1)
        for i, l in enumerate(lines):
            l["t"] = round(i * span, 2)
    plain = "\n".join(l["text"] for l in lines if l["text"])
    return {
        "title": title, "artist": artist, "album": album,
        "source": source, "lang": lang,
        "lines": lines, "plain": plain,
        "updated": round(time.time(), 1),
    }


def save_lyric(db: DB, song_id: int, song_path: str, payload: dict) -> None:
    fp = config.LYRICS_DIR / f"{song_id}.json"
    fp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")
    with db.tx() as c:
        c.execute("INSERT INTO lyrics(song_path,title,artist,album,source,lang,lines,plain,updated,song_id) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?) "
                  "ON CONFLICT(song_path) DO UPDATE SET title=excluded.title,artist=excluded.artist,"
                  "album=excluded.album,source=excluded.source,lang=excluded.lang,lines=excluded.lines,"
                  "plain=excluded.plain,updated=excluded.updated,song_id=excluded.song_id",
                  (song_path, payload["title"], payload["artist"], payload["album"], payload["source"],
                   payload["lang"], json.dumps(payload["lines"], ensure_ascii=False),
                   payload["plain"], payload["updated"], song_id))
        c.execute("UPDATE songs SET lyric_id=(SELECT id FROM lyrics WHERE song_path=?) WHERE id=?",
                  (song_path, song_id))


def load_lyric(db: DB, song_id: int) -> dict | None:
    fp = config.LYRICS_DIR / f"{song_id}.json"
    if fp.exists():
        try:
            return json.loads(fp.read_text("utf-8"))
        except Exception:
            pass
    r = db.one("SELECT * FROM lyrics WHERE song_id=?", (song_id,))
    if not r:
        r = db.one("SELECT l.* FROM lyrics l JOIN songs s ON s.path=l.song_path WHERE s.id=?", (song_id,))
    if not r:
        return None
    try:
        lines = json.loads(r["lines"])
    except Exception:
        lines = []
    return {"title": r["title"], "artist": r["artist"], "album": r["album"],
            "source": r["source"], "lang": r["lang"], "lines": lines, "plain": r["plain"],
            "updated": r["updated"]}


def attach_embedded(db: DB, song_id: int, song_path: str, title: str, artist: str,
                    album: str, raw: str, duration: float) -> bool:
    if not raw or not raw.strip():
        return False
    lines, meta = parse_lrc(raw)
    if not lines:
        return False
    payload = build_payload(title, artist, album, "embedded", lines, meta, duration)
    save_lyric(db, song_id, song_path, payload)
    return True


# ---------------------------------------------------------------- 外部导入
def _read_text(fp: Path) -> str | None:
    """国内 LRC 常见 GBK/GB18030，按 BOM -> utf-8 -> gb18030 依次尝试。"""
    try:
        raw = fp.read_bytes()
    except Exception:
        return None
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        try:
            return raw.decode("utf-16")
        except Exception:
            pass
    for enc in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def import_dir(db: DB, root: str) -> dict:
    """扫描目录里的 .lrc/.json 歌词，按标题+歌手匹配到歌曲。"""
    root = Path(root)
    stats = {"files": 0, "matched": 0, "created": 0}
    if not root.is_dir():
        return {"error": f"目录不存在: {root}"}
    songs = db.q("SELECT id,path,title,artist,duration FROM songs")
    index: dict[tuple[str, str], list[dict]] = {}
    loose: dict[str, list[dict]] = {}
    def norm(s): return re.sub(r"[\s\-_·.,'!?！？，。'\"“”（）()\[\]【】]", "", (s or "").lower())
    for s in songs:
        index.setdefault((norm(s["title"]), norm(s["artist"])), []).append(s)
        loose.setdefault(norm(s["title"]), []).append(s)

    for fp in root.rglob("*"):
        if not fp.is_file() or fp.suffix.lower() not in (".lrc", ".json", ".txt"):
            continue
        stats["files"] += 1
        text = _read_text(fp)
        if text is None:
            continue
        title = artist = album = ""
        lines: list[dict] = []
        meta: dict = {}
        pairs: list[tuple[str, str]] = []
        if fp.suffix.lower() == ".json":
            try:
                data = json.loads(text)
            except Exception:
                continue
            if isinstance(data, dict):
                title = data.get("title") or data.get("ti") or ""
                artist = data.get("artist") or data.get("ar") or ""
                album = data.get("album") or data.get("al") or ""
                raw_lines = data.get("lines") or data.get("lyrics") or []
                if isinstance(raw_lines, str):
                    lines, meta = parse_lrc(raw_lines)
                elif raw_lines and isinstance(raw_lines[0], dict):
                    lines = [{"t": float(x.get("t", x.get("time", -1))), "text": str(x.get("text", ""))}
                             for x in raw_lines]
            elif isinstance(data, list):
                lines = [{"t": float(x.get("t", -1)), "text": str(x.get("text", ""))} for x in data]
            if not title:
                title = fp.stem
            pairs = [(title, artist), (artist, title)]   # 字段顺序也可能反
        else:
            stem = fp.stem
            toks = None
            for sep in (" - ", " -", "- ", "&", "＆", "、", "_"):
                if sep in stem:
                    a, b = stem.split(sep, 1)
                    toks = (a.strip(), b.strip())
                    break
            pairs = [toks, toks[::-1]] if toks else [(stem, "")]
            lines, meta = parse_lrc(text)
        if not lines:
            continue
        # LRC 内 [ti:]/[ar:] 元数据优先于文件名猜测
        if meta.get("ti") or meta.get("ar"):
            pairs.insert(0, (meta.get("ti", ""), meta.get("ar", "")))
        cands: list[dict] = []
        for t, a in pairs:
            if not t:
                continue
            cands = index.get((norm(t), norm(a))) or loose.get(norm(t)) or []
            if cands:
                title, artist = t, a
                break
        if not cands:
            continue
        stats["matched"] += 1
        for s in cands[:1]:
            dur = s["duration"] or 0
            payload = build_payload(s["title"] or title, s["artist"] or artist, album,
                                    "import", lines, meta, dur)
            save_lyric(db, s["id"], s["path"], payload)
            stats["created"] += 1
    return stats


def search(db: DB, keyword: str, limit: int = 80) -> list[dict]:
    """按歌词全文搜索，返回带命中行的歌曲列表。"""
    kw = keyword.strip()
    if not kw:
        return []
    like = f"%{kw}%"
    rows = db.q(
        "SELECT s.id, s.title, s.artist, s.album, s.mood, s.mood_label, s.duration, s.path, "
        "       l.plain, l.lang, l.source, l.lines "
        "FROM lyrics l JOIN songs s ON s.path = l.song_path "
        "WHERE l.plain LIKE ? OR s.title LIKE ? OR s.artist LIKE ? "
        "ORDER BY (CASE WHEN s.title LIKE ? THEN 0 ELSE 1 END), length(l.plain) DESC "
        "LIMIT ?", (like, like, like, like, limit))
    out = []
    for r in rows:
        plain = r["plain"] or ""
        hits = []
        for ln in plain.splitlines():
            if kw in ln:
                hits.append(ln.strip())
            if len(hits) >= 4:
                break
        try:
            lines = json.loads(r["lines"] or "[]")
        except Exception:
            lines = []
        first_t = next((l["t"] for l in lines if kw in l.get("text", "")), None)
        out.append({"id": r["id"], "title": r["title"], "artist": r["artist"], "album": r["album"],
                    "mood": r["mood"], "mood_label": r["mood_label"], "duration": r["duration"],
                    "path": r["path"], "lang": r["lang"], "source": r["source"],
                    "hits": hits, "hit_time": first_t})
    return out
