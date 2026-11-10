"""曲库管理：扫描 -> 解析元数据 -> 后台增量分析特征 -> 自动重建氛围歌单。
所有耗时操作都在后台线程，HTTP 服务启动时只读数据库，因此秒开。
"""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import config, features, lyrics as lyr, moods, parse
from .db import DB

STATE = {
    "scanning": False, "scan_done": 0, "scan_total": 0, "scan_phase": "",
    "analyzing": False, "feat_done": 0, "feat_total": 0, "feat_failed": 0,
    "last_scan": 0.0, "last_rebuild": 0.0, "note": "",
}
_LOCK = threading.Lock()
FEAT_VER = 7          # 特征/分类算法版本；提升后自动重算全库


def _folder_of(path: str, cfg: dict) -> str:
    """歌曲所在目录链（含扫描根名），作为分类线索，如 '纯音乐' 或 '方格/网易云本地'。"""
    parts: list[str] = []
    d = os.path.dirname(os.path.abspath(path))
    for _ in range(6):
        parent, name = os.path.split(d)
        if not name or name == parent:
            break
        parts.append(name)
        d = parent
        for root in cfg.get("scan_roots", []):
            if os.path.normcase(os.path.abspath(root)) == os.path.normcase(d):
                parts.append(os.path.basename(os.path.normpath(root)))   # 根名也要保留
                parts.reverse()
                return "/".join(parts)
    parts.reverse()
    return "/".join(parts)


def _set(**kw) -> None:
    with _LOCK:
        STATE.update(kw)


# ---------------------------------------------------------------- 扫描
def _walk(roots: list[str], excludes: list[str], excl_roots: list[str]) -> list[str]:
    out: list[str] = []
    ex = set(x.lower() for x in excludes)
    exr = set(os.path.normcase(os.path.normpath(r)) for r in excl_roots)
    for root in roots:
        root = os.path.normpath(root)
        if os.path.normcase(root) in exr or not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
            dirnames[:] = [d for d in dirnames
                           if d.lower() not in ex and not d.startswith("$")
                           and os.path.normcase(os.path.join(dirpath, d)) not in exr]
            for fn in filenames:
                if os.path.splitext(fn)[1].lower() in config.AUDIO_EXTS:
                    out.append(os.path.join(dirpath, fn))
    return out


def scan(db: DB, cfg: dict, quick: bool = False) -> dict:
    """登记文件（快，只读标签）。已存在且未修改的文件跳过。"""
    if STATE["scanning"]:
        return {"skipped": "扫描进行中"}
    _set(scanning=True, scan_phase="遍历目录", note="")
    t0 = time.time()
    try:
        files = _walk(cfg["scan_roots"], cfg["exclude_dirs"], cfg.get("exclude_roots", []))
        _set(scan_total=len(files), scan_done=0, scan_phase="读取标签")
        have = {}
        orig: dict[str, str] = {}
        for r in db.q("SELECT path,mtime,size FROM songs"):
            k = os.path.normcase(r["path"])
            have[k] = (r["mtime"], r["size"])
            orig[k] = r["path"]
        new, changed, seen = [], [], set()
        for p in files:
            try:
                st = os.stat(p)
            except OSError:
                continue
            pn = os.path.normpath(p)
            k = os.path.normcase(pn)
            seen.add(k)
            prev = have.get(k)
            if prev and abs(prev[0] - st.st_mtime) < 1.5 and prev[1] == st.st_size:
                continue
            (changed if prev else new).append(pn)

        rows = []
        for i, p in enumerate(new + changed):
            info = parse.parse_song(p)
            rows.append((info.path, info.filename, info.title, info.artist, info.album,
                         info.ext, info.size, info.extra["mtime"], info.duration,
                         info.bitrate, info.samplerate, int(info.has_meta), time.time(),
                         info.embedded_lyric, info.cover, _folder_of(info.path, cfg)))
            if not quick and (i + 1) % 40 == 0:
                _set(scan_done=i + 1)
                time.sleep(0)

        extras = []
        with db.tx() as c:
            for (path, name, title, artist, album, ext, size, mtime, dur, br, sr, hm, added,
                 emb, cover, folder) in rows:
                c.execute(
                    "INSERT INTO songs(path,name,title,artist,album,ext,size,mtime,duration,bitrate,"
                    "samplerate,has_meta,added,folder,analyzed) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0) "
                    "ON CONFLICT(path) DO UPDATE SET name=excluded.name,title=excluded.title,"
                    "artist=excluded.artist,album=excluded.album,ext=excluded.ext,size=excluded.size,"
                    "mtime=excluded.mtime,duration=excluded.duration,bitrate=excluded.bitrate,"
                    "samplerate=excluded.samplerate,has_meta=excluded.has_meta,folder=excluded.folder",
                    (path, name, title, artist, album, ext, size, mtime, dur, br, sr, hm, added, folder))
                if cover or (emb and emb.strip()):
                    extras.append((path, title, artist, album, dur, emb, cover))
        # 封面/内嵌歌词在事务外处理（save_lyric 自带事务，嵌套会死锁）
        for (path, title, artist, album, dur, emb, cover) in extras:
            sid = db.one("SELECT id FROM songs WHERE path=?", (path,))
            if not sid:
                continue
            if cover:
                _save_cover(sid["id"], cover)
            if emb and emb.strip():
                _try_embedded_lyric(db, sid["id"], path, title, artist, album, emb, dur)
        # 清理消失的文件（have 的键已 normcase，需取回原始路径再删）
        gone = [orig[k] for k in have if k not in seen]
        if gone:
            with db.tx() as c:
                c.executemany("DELETE FROM songs WHERE path=?", [(p,) for p in gone])
                c.execute("DELETE FROM playlist_items WHERE song_id NOT IN (SELECT id FROM songs)")
                c.execute("DELETE FROM lyrics WHERE song_path NOT IN (SELECT path FROM songs)")
        # 补齐历史缺失的 folder
        miss = db.q("SELECT id,path FROM songs WHERE folder IS NULL OR folder=''")
        if miss:
            with db.tx() as c:
                c.executemany("UPDATE songs SET folder=? WHERE id=?",
                              [(_folder_of(r["path"], cfg), r["id"]) for r in miss])
        _set(last_scan=time.time(), scan_done=len(new + changed))
        n_analyzed = db.one("SELECT COUNT(*) n FROM songs WHERE analyzed=1")["n"]
        _set(feat_total=len(new + changed) + n_analyzed)
        _set(note=f"新增/更新 {len(new)+len(changed)} 首，共 {len(files)} 首，耗时 {time.time()-t0:.1f}s")
        return {"new": len(new), "changed": len(changed), "removed": len(gone), "total": len(files)}
    finally:
        _set(scanning=False, scan_phase="")


def _save_cover(song_id: int, data: bytes) -> None:
    try:
        fp = config.COVER_DIR / f"{song_id}.jpg"
        if not fp.exists() or fp.stat().st_size < 512:
            fp.write_bytes(data)
    except Exception:
        pass


def _try_embedded_lyric(db, song_id, path, title, artist, album, emb, dur) -> None:
    if not emb or not emb.strip():
        return
    if db.one("SELECT id FROM lyrics WHERE song_path=?", (path,)):
        return
    try:
        lines, meta = lyr.parse_lrc(emb)
        if len(lines) >= 4:
            lyr.save_lyric(db, song_id, path,
                           lyr.build_payload(title, artist, album, "embedded", lines, meta, dur or 0))
    except Exception:
        pass


# ---------------------------------------------------------------- 分析
def reclassify_only(db: DB, cfg: dict) -> int:
    """只改了分类规则（moods.py）时：用库里已存的特征重新打分，秒级完成，不重新解码。"""
    rows = db.q("SELECT id,path,title,artist,album,tempo,energy,danceability,acoustic,"
                "valence,brightness,roughness,flatness,vocal,tonality,dynamic,mod "
                "FROM songs WHERE analyzed=1 AND failed=0 AND v_raw IS NOT NULL AND a_raw IS NOT NULL")
    n = 0
    with db.tx() as c:
        for s in rows:
            folder = _folder_of(s["path"], cfg)
            feat = {k: (s[k] if s[k] is not None else 0.0)
                    for k in ("tempo", "energy", "danceability", "acoustic", "valence",
                              "brightness", "roughness", "flatness", "vocal", "tonality",
                              "dynamic", "mod")}
            key, reason, _ = moods.classify(feat, s["title"] or "", s["artist"] or "",
                                            s["album"] or "", folder)
            c.execute("UPDATE songs SET mood=?,mood_label=?,mood_reason=?,feat_ver=?,folder=? WHERE id=?",
                      (key, moods.MOOD_MAP[key][0], reason, FEAT_VER, folder, s["id"]))
            n += 1
    rebuild_playlists(db, cfg)
    return n


def analyze_pending(db: DB, cfg: dict, limit: int = 0, wake: bool = True) -> int:
    """后台线程池分析未分析歌曲。返回提交数量。"""
    todo = db.q("SELECT id,path,title,artist,album,duration,folder FROM songs "
                "WHERE (analyzed=0 OR feat_ver<? ) AND failed=0 "
                "ORDER BY size ASC" + (f" LIMIT {int(limit)}" if limit else ""), (FEAT_VER,))
    if not todo:
        return 0
    if STATE["analyzing"]:
        return 0
    workers = max(1, int(cfg.get("analyze_workers", 2)))
    secs = float(cfg.get("analyze_seconds", 100))

    def run():
        _set(analyzing=True, feat_total=len(todo) + STATE["feat_done"], note=f"分析中 · {workers} 线程")
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_one, db, s, secs) for s in todo]
            for fu in futs:
                try:
                    fu.result()
                except Exception:
                    pass
                done += 1
                _set(feat_done=STATE["feat_done"] + 1)
                if done % 25 == 0:
                    rebuild_playlists(db, cfg, quiet=True)
        rebuild_playlists(db, cfg)
        _set(analyzing=False, note=f"分析完成，共 {done} 首")

    t = threading.Thread(target=run, name="analyzer", daemon=True)
    t.start()
    return len(todo)


def _one(db: DB, s: dict, secs: float) -> None:
    sid, path = s["id"], s["path"]
    try:
        feat = features.extract(path, secs)
        key, reason, scores = moods.classify(feat, s["title"] or "", s["artist"] or "",
                                             s["album"] or "", s.get("folder") or "")
        label = moods.MOOD_MAP[key][0]
        with db.tx() as c:
            c.execute("UPDATE songs SET tempo=?,energy=?,danceability=?,acoustic=?,valence=?,brightness=?,"
                      "roughness=?,flatness=?,vocal=?,tonality=?,dynamic=?,mod=?,v_raw=?,a_raw=?,"
                      "mood=?,mood_label=?,mood_reason=?,analyzed=1,feat_ver=?, duration=CASE "
                      "WHEN duration=0 OR duration IS NULL THEN ? ELSE duration END, failed=0 WHERE id=?",
                      (feat["tempo"], feat["energy"], feat["danceability"], feat["acoustic"],
                       feat["valence"], feat["brightness"], feat["roughness"], feat["flatness"],
                       feat["vocal"], feat["tonality"], feat["dynamic"], feat["mod"],
                       feat["v_raw"], feat["a_raw"], key, label, reason, FEAT_VER, feat["duration"], sid))
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"[:200]
        with db.tx() as c:
            c.execute("UPDATE songs SET analyzed=2, failed=1, mood_reason=? WHERE id=?", (msg, sid))
        with _LOCK:
            STATE["feat_failed"] = STATE.get("feat_failed", 0) + 1


# ---------------------------------------------------------------- 歌单
def rebuild_playlists(db: DB, cfg: dict, quiet: bool = False) -> dict:
    """按 mood 重建自动歌单；每个歌单内按能量/舞曲度排序，保证听感连贯。"""
    cap = int(cfg.get("max_playlist_songs", 400))
    with db.tx() as c:
        c.execute("DELETE FROM playlists WHERE auto=1")
        c.execute("DELETE FROM playlist_items WHERE playlist_id NOT IN "
                  "(SELECT id FROM playlists WHERE auto=0)")
        ids = {}
        for i, m in enumerate(moods.MOODS):
            key, name, emoji, desc = m
            cur = c.execute("INSERT INTO playlists(key,name,emoji,description,auto,sort,created) "
                            "VALUES(?,?,?,?,1,?,?)", (key, name, emoji, desc, i, time.time()))
            ids[key] = cur.lastrowid
        for key, pid in ids.items():
            rows = c.execute(
                "SELECT id FROM songs WHERE mood=? AND analyzed=1 "
                "ORDER BY energy DESC, danceability DESC LIMIT ?", (key, cap)).fetchall()
            c.executemany("INSERT OR IGNORE INTO playlist_items(playlist_id,song_id,pos) VALUES(?,?,?)",
                          [(pid, r["id"], i) for i, r in enumerate(rows)])
        # 全部 / 未分析 两个辅助歌单
        allpid = c.execute("INSERT OR IGNORE INTO playlists(key,name,emoji,description,auto,sort,created) "
                           "VALUES('all','全部歌曲','🎵','曲库里的所有歌',1,90,?)", (time.time(),)).lastrowid
        c.execute("DELETE FROM playlist_items WHERE playlist_id=?", (allpid,))
        c.execute("INSERT OR IGNORE INTO playlist_items(playlist_id,song_id,pos) "
                  "SELECT ?, id, row_number() OVER (ORDER BY title) FROM songs", (allpid,))
        pend = db.one("SELECT COUNT(*) n FROM songs WHERE analyzed=0")["n"]
        if pend:
            pp = c.execute("INSERT OR IGNORE INTO playlists(key,name,emoji,description,auto,sort,created) "
                           "VALUES('pending','待分析','⏳',?,1,91,?)",
                           (f"后台音频分析进度中的歌曲（{pend} 首）", time.time())).lastrowid
            c.execute("DELETE FROM playlist_items WHERE playlist_id=?", (pp,))
            c.execute("INSERT OR IGNORE INTO playlist_items(playlist_id,song_id,pos) "
                      "SELECT ?, id, row_number() OVER (ORDER BY size) FROM songs WHERE analyzed=0", (pp,))
    counts = {r["key"]: r["n"] for r in db.q(
        "SELECT p.key, COUNT(pi.song_id) n FROM playlists p LEFT JOIN playlist_items pi "
        "ON pi.playlist_id=p.id WHERE p.auto=1 GROUP BY p.key")}
    if not quiet:
        _set(last_rebuild=time.time())
    return counts


def create_smart(db: DB, name: str, emoji: str, where: str, order: str = "energy DESC",
                 limit: int = 300) -> dict:
    """用户自定义条件歌单（也标 auto=0，重建时保留）。"""
    key = "user_" + str(int(time.time() * 1000) % 10**10)
    with db.tx() as c:
        pid = c.execute("INSERT INTO playlists(key,name,emoji,description,auto,sort,created) "
                        "VALUES(?,?,?,?,0,95,?)", (key, name, emoji, where, time.time())).lastrowid
        rows = c.execute(f"SELECT id FROM songs WHERE analyzed=1 AND ({where}) "
                         f"ORDER BY {order} LIMIT ?", (int(limit),)).fetchall()
        c.executemany("INSERT OR IGNORE INTO playlist_items(playlist_id,song_id,pos) VALUES(?,?,?)",
                      [(pid, r["id"], i) for i, r in enumerate(rows)])
    return {"id": pid, "key": key, "name": name, "count": len(rows)}
