"""音质评判 + 自动去重 + 安全清除（默认只识别与标记；清除须逐项确认并移入回收站）。

两层设计，保证冷启动仍然秒开：
  L1 元数据层：只用库内 ext / size / duration / bitrate / samplerate，零解码，全库 < 0.2s。
  L2 频谱层  ：ffmpeg 解码 9 秒 / 44.1kHz，估计高频截止，识别「有损转码的假无损」，按需后台跑。

判重同样两层：
  same-song：规范化 歌名 + 歌手，再按时长容差聚类 -> 同曲不同版本（用于换无损）
  exact    ：size 相同 + 头尾指纹相同 -> 完全相同的文件（可清理其一）

评分是规则打分，不是听感评测；判定依据全部写进 q_reason，便于人工复核。
"""
from __future__ import annotations

import hashlib
import io
import csv
import json
import os
import re
import shutil
import subprocess
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor

import numpy as np

Q_VER = 1                      # 评分规则版本；改规则后 +1，下次 refresh 全库重算
LOSSLESS = {".flac", ".ape", ".wav", ".wv", ".tta", ".dsf", ".dsd", ".aiff", ".aif"}
LOSSY = {".mp3", ".aac", ".m4a", ".ogg", ".oga", ".opus", ".wma", ".mp4"}

# 实测校准：本机 wav/flac 真无损截止全部达 21750Hz；mp3 多在 15750~20750Hz。
# 标称无损但截止 < 20000Hz -> 判「疑似假无损」（保守阈值，宁漏勿误）。
FAKE_CUTOFF_HZ = 20000
DEEP_SR = 44100

STATE = {"running": False, "done": 0, "total": 0, "fake": 0, "note": "", "last": 0.0}
_LOCK = threading.Lock()


def _set(**kw) -> None:
    with _LOCK:
        STATE.update(kw)


def deep_state() -> dict:
    with _LOCK:
        return dict(STATE)


# ---------------------------------------------------------------- 规范化 / 判重键
# 只剔除「音质/来源」标记；Live / Remix / DJ / 伴奏等版本标记必须保留，避免把不同录音误合并。
_QUALITY_TAG = re.compile(
    r"[\[\(【]?\s*(?:flac|ape|wav|wv|tta|dsf|dsd|mp3|aac|ogg|hq|sq|mqms2?|mq|"
    r"320k?|256k?|192k?|128k?|64k?|无损|高音质|hi-?res|dolby|atmos|空间音频|"
    r"16bit|24bit|44\.1k?|48k?|96k?)\s*[\]\)】]?", re.I)
_PUNCT = re.compile(r"[^\w]+", re.UNICODE)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = _QUALITY_TAG.sub(" ", s)
    return _PUNCT.sub("", s)


def _dup_base(r: dict) -> str:
    """同曲键：规范化歌名 + 歌手前 16 字。歌名过短则不成组，避免误合并。"""
    t = _norm(r.get("title") or "") or _norm(r.get("name") or "")
    if len(t) < 2:
        return ""
    a = _norm(r.get("artist") or "")
    return f"{t[:60]}|{a[:16]}" + ("|?" if not a else "")


def _duration_close(a: float, b: float) -> bool:
    """同一录音的不同格式时长应几乎一致；现场版/remix 通常差更多。"""
    if a <= 0 or b <= 0:
        return True                      # 元数据缺失时不因时长拆组
    return abs(a - b) <= max(6.0, min(a, b) * 0.10)


def _fingerprint(path: str, size: int) -> str:
    """只对 size 相同的候选读头尾各 64KB，避免全库 IO。"""
    try:
        h = hashlib.md5(str(size).encode())
        with open(path, "rb") as f:
            h.update(f.read(65536))
            if size > 131072:
                f.seek(-65536, os.SEEK_END)
                h.update(f.read(65536))
        return h.hexdigest()
    except OSError:
        return ""


# ---------------------------------------------------------------- 音质评分
def _kbps(r: dict) -> float:
    """优先用 体积/时长 推有效码率（对 wav/flac/ape 更真实），异常时回退标签码率。"""
    dur = r.get("duration") or 0
    eff = (r.get("size") or 0) * 8.0 / dur / 1000.0 if dur > 0 else 0.0
    tag = (r.get("bitrate") or 0) / 1000.0
    if 0 < eff < 6000:
        return eff
    return tag if 0 < tag < 6000 else 0.0


def score_row(r: dict, cutoff: int | None = None) -> dict:
    """综合评分（0~100）+ 档位 + 可读依据。cutoff 为 L2 频谱检测结果（Hz）。"""
    ext = (r.get("ext") or "").lower()
    sr = int(r.get("samplerate") or 0)
    k = _kbps(r)
    lossless = ext in LOSSLESS
    fake, label, s = 0, "未知", 0.0

    if lossless:
        s, label = 84.0, "真无损"
        if sr >= 96000:
            s += 8
        elif sr >= 48000:
            s += 4
        if 0 < k < 500:                 # 无损不该这么小，多半是低码率转码
            s -= 25
        elif k >= 900:
            s += 3
    elif k > 0:
        if k >= 320:
            s, label = 62 + min(13.0, (k - 320) / 680 * 13), "高码率有损"
        elif k >= 192:
            s, label = 48 + (k - 192) / 128 * 14, "有损"
        elif k >= 128:
            s, label = 34 + (k - 128) / 64 * 14, "低码率"
        else:
            s, label = max(8.0, k / 128 * 34), "低码率"
        # .m4a 可能是 ALAC（Apple Lossless），体积会明显偏大
        if ext == ".m4a" and k >= 700:
            s, label = 86.0, "真无损"
    else:
        s, label = 0.0, "未知"

    # L2：截止频率是硬证据——44.1kHz 真无损在 21kHz 以上仍有能量，
    # 有损转码会留下低通砖墙。cutoff<20kHz 即判「疑似」，理由里写明数值供人工复核。
    if cutoff:
        if label == "真无损":
            if cutoff < FAKE_CUTOFF_HZ:
                label, fake = "疑似假无损", 1
                s = min(s, 28 + max(0, cutoff - 10000) / 10000 * 17)
            elif cutoff >= 21000:
                s = min(100.0, s + 4)
        else:
            s = min(100.0, s + max(-4.0, min(4.0, (cutoff - 16000) / 1000)))

    s = round(max(0.0, min(100.0, s)), 1)
    reason = f"{ext.lstrip('.') or '?'} · {k:.0f} kbps"
    if sr:
        reason += f" · {sr / 1000:.1f} kHz"
    if cutoff:
        reason += f" · 高频截止 {cutoff / 1000:.1f} kHz"
    if fake:
        reason += " · 高频呈砖墙式截断，疑似由有损转码"
    return {"q_score": s, "q_label": label, "q_reason": reason, "q_kbps": int(k),
            "q_sr": sr, "q_fake": fake}


# ---------------------------------------------------------------- L1：全库即时重算
def refresh(db, force: bool = False) -> dict:
    """零解码重算评分 + 去重。启动时自动跑一次，扫描后也会跑。"""
    rows = db.q("SELECT * FROM songs")
    upd = []
    for r in rows:
        # q_brick 来自 L2 频谱检测：refresh 重算分数时必须带上，否则假无损判定会被抹掉
        s = score_row(r, r.get("q_cutoff") or None)
        upd.append((s["q_score"], s["q_label"], s["q_reason"], s["q_kbps"], s["q_sr"],
                    s["q_fake"], Q_VER, r["id"]))
    with db.tx() as c:
        c.executemany("UPDATE songs SET q_score=?,q_label=?,q_reason=?,q_kbps=?,q_sr=?,"
                      "q_fake=?,q_ver=? WHERE id=?", upd)
    d = mark_duplicates(db)
    return {"scored": len(rows), "force": force, **d}


def mark_duplicates(db) -> dict:
    """写入 dup_key / dup_best / exact_key / q_advice。"""
    rows = db.q("SELECT id,title,artist,name,duration,size,path,q_score,ext FROM songs")

    # 1) 同曲分组：先按规范化键，再在组内按时长聚类
    buckets: dict[str, list[dict]] = {}
    for r in rows:
        k = _dup_base(r)
        if k:
            buckets.setdefault(k, []).append(r)
    groups: list[tuple[str, list[dict]]] = []
    for k, ms in buckets.items():
        ms.sort(key=lambda x: x["duration"] or 0)
        clusters: list[list[dict]] = []
        for m in ms:
            for cl in clusters:
                if _duration_close(m["duration"] or 0, cl[0]["duration"] or 0):
                    cl.append(m)
                    break
            else:
                clusters.append([m])
        for i, cl in enumerate(clusters):
            groups.append((k if len(clusters) == 1 else f"{k}#{i + 1}", cl))

    # 2) 完全相同文件：size 相同的组内做头尾指纹
    by_size: dict[int, list[dict]] = {}
    for r in rows:
        if r["size"]:
            by_size.setdefault(r["size"], []).append(r)
    exact_of: dict[int, str] = {}
    exact_groups = 0
    for size, ms in by_size.items():
        if len(ms) < 2:
            continue
        fps: dict[str, list[dict]] = {}
        for m in ms:
            fp = _fingerprint(m["path"], size)
            if fp:
                fps.setdefault(fp, []).append(m)
        for fp, same in fps.items():
            if len(same) < 2:
                continue
            exact_groups += 1
            key = f"x{size}_{fp[:12]}"
            for m in same:
                exact_of[m["id"]] = key

    # 3) 生成建议（只标记，不删文件）
    updates = []
    dup_groups = dup_songs = upgrade_groups = 0
    for key, ms in groups:
        multi = len(ms) > 1
        if multi:
            dup_groups += 1
            dup_songs += len(ms)
        best = max(ms, key=lambda m: (m["q_score"] or 0, -m["id"]))
        if (best["q_score"] or 0) < 80:
            upgrade_groups += 1
        for m in ms:
            ex = exact_of.get(m["id"], "")
            if ex and multi:
                advice = "完全相同文件，可清理其一" if m["id"] != best["id"] else "保留（最佳版本）"
            elif multi:
                advice = "可清理低质副本" if m["id"] != best["id"] else "保留（最佳版本）"
            elif (m["q_score"] or 0) < 80:
                advice = "建议换无损"
            else:
                advice = "保留"
            updates.append((key, int(m["id"] == best["id"]), ex, advice, m["id"]))
    with db.tx() as c:
        c.executemany("UPDATE songs SET dup_key=?,dup_best=?,exact_key=?,q_advice=? WHERE id=?",
                      updates)
    return {"dup_groups": dup_groups, "dup_songs": dup_songs,
            "exact_groups": exact_groups, "upgrade_groups": upgrade_groups}


# ---------------------------------------------------------------- L2：频谱深度检测
def _decode_hi(path: str, seconds: int = 9, sr: int = DEEP_SR) -> np.ndarray | None:
    ff = shutil.which(os.environ.get("FFMPEG_BIN", "ffmpeg"))
    if not ff:
        return None
    for ss in (8, 0):                    # 跳过前奏；短曲回退到开头
        cmd = [ff, "-v", "error", "-nostdin", "-ss", str(ss), "-t", str(seconds),
               "-i", path, "-f", "f32le", "-acodec", "pcm_f32le", "-ac", "1",
               "-ar", str(sr), "-"]
        try:
            raw = subprocess.run(cmd, capture_output=True, timeout=45).stdout
            y = np.frombuffer(raw, dtype=np.float32).copy()
            if y.size >= sr * 2:
                return y
        except Exception:
            pass
    return None


def spectrum_info(path: str) -> dict:
    """返回 {cutoff: 高频截止 Hz, hf: 15~19kHz 相对中频 dB, brick: 是否砖墙式截断}。"""
    y = _decode_hi(path)
    if y is None:
        return {}
    nper, hop = 4096, 2048
    if y.size < nper * 2:
        return {}
    win = np.hanning(nper).astype(np.float32)
    n = 1 + (y.size - nper) // hop
    idx = np.arange(nper)[None, :] + hop * np.arange(n)[:, None]
    frames = y[idx] * win
    S = np.abs(np.fft.rfft(frames, axis=1))
    P = (S * S).mean(axis=0) + 1e-20
    freqs = np.fft.rfftfreq(nper, 1.0 / DEEP_SR)
    db = 10 * np.log10(P)
    ref = db[(freqs >= 1000) & (freqs <= 5000)].mean()
    d = db - ref

    # 从 12kHz 起，以 250Hz 步进找最后一个仍有能量的频带
    cutoff = 12000
    for f0 in np.arange(12000, min(DEEP_SR / 2 - 500, 22000), 250):
        m = (freqs >= f0) & (freqs < f0 + 500)
        if not m.any():
            break
        if np.percentile(d[m], 75) > -70:
            cutoff = int(f0 + 250)
    band = (freqs >= 15000) & (freqs <= 19000)
    hf = float(np.percentile(d[band], 75)) if band.any() else -99.0
    # brick 为辅助指标（高频能量塌缩），不作为硬判据：素材本身高频少时 hf 会偏低
    brick = int(cutoff < FAKE_CUTOFF_HZ and hf < -35)
    return {"cutoff": cutoff, "hf": round(hf, 1), "brick": brick}


def deep_scan(db, limit: int = 0, workers: int = 2) -> dict:
    """后台检测无损候选的高频截止，识别假无损。可重复调用（已有结果会跳过）。"""
    if STATE["running"]:
        return {"ok": False, "reason": "检测正在进行中", **deep_state()}
    exts = ",".join("'" + e + "'" for e in sorted(LOSSLESS))
    rows = db.q(f"SELECT * FROM songs "
                f"WHERE q_cutoff IS NULL AND (ext IN ({exts}) OR q_label='真无损') "
                f"ORDER BY id" + (f" LIMIT {int(limit)}" if limit else ""))
    if not rows:
        return {"ok": True, "queued": 0, "reason": "没有待检测文件", **deep_state()}
    _set(running=True, done=0, total=len(rows), fake=0, note="频谱检测中")

    def run():
        done = fake = 0
        with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
            for start in range(0, len(rows), 20):
                batch = rows[start:start + 20]
                infos = list(ex.map(lambda r: spectrum_info(r["path"]), batch))
                with db.tx() as c:
                    for r, info in zip(batch, infos):
                        # 原始采样率低于 44.1kHz 时 Nyquist 本身就低，不能据此判假无损
                        if r.get("samplerate") and r["samplerate"] < 44100:
                            c.execute("UPDATE songs SET q_cutoff=0,q_hf=0,q_brick=0,q_fake=0 WHERE id=?",
                                      (r["id"],))
                            done += 1
                            continue
                        cutoff = info.get("cutoff") or 0   # 解码失败也占位，避免反复重试
                        r2 = dict(r)
                        r2["_brick"] = info.get("brick")
                        is_fake = score_row(r2, cutoff or None)["q_fake"]
                        fake += is_fake
                        c.execute("UPDATE songs SET q_cutoff=?,q_hf=?,q_brick=?,q_fake=? WHERE id=?",
                                  (cutoff, info.get("hf", 0), info.get("brick", 0),
                                   is_fake, r["id"]))
                        done += 1
                _set(done=done, fake=fake)
        refresh(db)                     # 用新 cutoff 重排全库分数与重复组
        _set(running=False, note=f"检测完成：{done} 首，疑似假无损 {fake} 首", last=time.time())

    threading.Thread(target=run, name="quality-deep", daemon=True).start()
    return {"ok": True, "queued": len(rows), **deep_state()}


# ---------------------------------------------------------------- 查询
_UPGRADE_IDS = """
SELECT id FROM songs WHERE id IN (
  SELECT (SELECT id FROM songs x WHERE x.dup_key=g.dup_key
          ORDER BY x.q_score DESC, x.id LIMIT 1)
  FROM (SELECT dup_key FROM songs WHERE dup_key IS NOT NULL
        GROUP BY dup_key HAVING MAX(q_score)<80) g
)
"""


def overview(db) -> dict:
    r = db.one("SELECT COUNT(*) total,"
               " SUM(q_label='真无损') lossless,"
               " SUM(q_fake=1) fake,"
               " SUM(q_cutoff IS NOT NULL AND q_cutoff>0) deep_done,"
               " SUM(q_score>=80) good,"
               " SUM(q_score<80) need_upgrade,"
               " SUM(ext IN ('.flac','.ape','.wav','.wv','.tta','.dsf','.dsd','.aiff','.aif') "
               "     AND (samplerate IS NULL OR samplerate=0 OR samplerate>=44100)) deep_candidates,"
               " SUM(CASE WHEN q_advice LIKE '%可清理%' THEN size ELSE 0 END) reclaim_bytes "
               "FROM songs") or {}
    d = db.one("SELECT COUNT(*) n FROM (SELECT dup_key FROM songs WHERE dup_key IS NOT NULL "
               "GROUP BY dup_key HAVING COUNT(*)>1)") or {}
    e = db.one("SELECT COUNT(*) n FROM (SELECT exact_key FROM songs WHERE exact_key IS NOT NULL "
               "AND exact_key<>'' GROUP BY exact_key HAVING COUNT(*)>1)") or {}
    u = db.one(f"SELECT COUNT(*) n FROM songs WHERE id IN ({_UPGRADE_IDS})") or {}
    st = deep_state()
    # 注意：deep_state() 里也有 total/fake，不能直接 ** 展开覆盖统计值
    return {**r, "dup_groups": d["n"], "exact_groups": e["n"], "upgrade_songs": u["n"],
            "deep_running": st["running"], "deep_progress": st["done"],
            "deep_total": st["total"], "deep_note": st["note"]}


def list_rows(db, kind: str = "upgrade", q: str = "", limit: int = 200, offset: int = 0) -> dict:
    where, args = [], []
    if kind == "upgrade":
        where.append(f"id IN ({_UPGRADE_IDS})")
    elif kind == "dup":
        where.append("dup_key IN (SELECT dup_key FROM songs WHERE dup_key IS NOT NULL "
                     "GROUP BY dup_key HAVING COUNT(*)>1)")
    elif kind == "fake":
        where.append("q_fake=1")
    elif kind == "lossy":
        where.append("q_score<80")
    if q:
        where.append("(title LIKE ? OR artist LIKE ? OR name LIKE ?)")
        args += [f"%{q}%"] * 3
    wsql = "WHERE " + " AND ".join(where) if where else ""
    total = db.one(f"SELECT COUNT(*) n FROM songs {wsql}", args)["n"]
    order = {"dup": "dup_key, q_score DESC", "score": "q_score DESC, id",
             "worst": "q_score ASC, id"}.get(kind, "q_score ASC, id")
    if kind == "dup":
        order = "dup_key, q_score DESC, id"
    elif kind == "fake":
        order = "q_score ASC, id"
    elif kind == "all":
        order = "q_score DESC, id"
    rows = db.q(f"SELECT id,title,artist,ext,duration,size,path,q_score,q_label,q_reason,"
                f"q_kbps,q_sr,q_cutoff,q_fake,q_advice,dup_key,dup_best,exact_key "
                f"FROM songs {wsql} ORDER BY {order} LIMIT ? OFFSET ?",
                args + [min(int(limit), 2000), int(offset)])
    if kind == "upgrade":
        for r in rows:            # 该组最高分仍 <80，代表整组都没有无损版本
            r["suggest"] = "建议换无损（当前无无损版本）"
    return {"rows": rows, "total": total}


def duplicate_groups(db, limit: int = 100) -> dict:
    rows = db.q("SELECT id,title,artist,ext,duration,size,path,q_score,q_label,q_reason,"
                "q_kbps,q_cutoff,q_advice,dup_key,dup_best,exact_key FROM songs "
                "WHERE dup_key IN (SELECT dup_key FROM songs WHERE dup_key IS NOT NULL "
                "GROUP BY dup_key HAVING COUNT(*)>1) ORDER BY dup_key, q_score DESC, id")
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["dup_key"], []).append(r)
    out = []
    for k, ms in groups.items():
        out.append({"key": k, "title": ms[0]["title"], "artist": ms[0]["artist"],
                    "members": ms,
                    "reclaim": sum(m["size"] or 0 for m in ms if not m["dup_best"])})
    out.sort(key=lambda g: (-len(g["members"]), -g["reclaim"]))
    return {"groups": out[:int(limit)], "total": len(out)}


def export_csv(db, kind: str = "upgrade") -> bytes:
    if kind == "dup":
        data = duplicate_groups(db, limit=10000)["groups"]
        rows = [(g["title"], g["artist"], m["ext"], m["q_kbps"], m["q_sr"],
                 m["q_score"], m["q_label"], "保留" if m["dup_best"] else "可清理", m["path"])
                for g in data for m in g["members"]]
        head = ("歌名", "歌手", "格式", "码率(kbps)", "采样率(Hz)", "评分", "档位", "建议", "路径")
    else:
        d = list_rows(db, "upgrade" if kind == "upgrade" else "all", limit=10000)
        rows = [(r["title"], r["artist"], r["ext"], r["q_kbps"], r["q_sr"],
                 r["q_score"], r["q_label"], r.get("suggest") or r["q_advice"], r["path"])
                for r in d["rows"]]
        head = ("歌名", "歌手", "格式", "码率(kbps)", "采样率(Hz)", "评分", "档位", "建议", "路径")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(head)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")   # BOM，Excel 直接打开不乱码


# ---------------------------------------------------------------- 安全清除
# 设计原则：绝不直接删除。文件一律移入 Windows 回收站（可还原），且
#   1) 只允许清理「同曲组内非最佳版本」，每组至少保留一个最佳版本；
#   2) 路径必须位于配置的 scan_roots 之内，且为已登记的音频文件；
#   3) 单批最多 10 首；执行前必须先预览，并携带服务端签发的确认令牌；
#   4) 每次操作写入 data/trash_log.jsonl，含原路径与保留版本，便于找回。
try:
    from send2trash import send2trash as _send2trash
except Exception:                                  # 未安装时降级为隔离目录
    _send2trash = None

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
TRASH_LOG = os.path.join(_DATA_DIR, "trash_log.jsonl")
TRASH_FALLBACK_DIR = os.path.join(_DATA_DIR, "trash")
TRASH_BATCH_MAX = 10
_TRASH_SECRET = os.urandom(16).hex()
_AUDIO_EXT = LOSSLESS | LOSSY


def _clean_ids(ids) -> list[int]:
    out = []
    for x in ids or []:
        try:
            i = int(x)
        except (TypeError, ValueError):
            continue
        if i > 0 and i not in out:
            out.append(i)
    return sorted(out)


def _in_roots(path: str, cfg: dict) -> bool:
    p = os.path.normcase(os.path.realpath(path))
    for root in cfg.get("scan_roots", []):
        r = os.path.normcase(os.path.realpath(root))
        if p.startswith(r.rstrip(os.sep) + os.sep):
            return True
    return False


def _trash_token(ids: list[int]) -> str:
    raw = "|".join(str(i) for i in sorted(ids)) + "|" + _TRASH_SECRET
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def preview_trash(db, ids, cfg) -> dict:
    """只做校验与清单生成，绝不碰文件。"""
    ids = _clean_ids(ids)
    if not ids:
        return {"ok": False, "error": "尚未选择要清理的文件"}
    if len(ids) > TRASH_BATCH_MAX:
        return {"ok": False, "error": f"单批最多 {TRASH_BATCH_MAX} 首，请分批操作"}
    ph = ",".join("?" * len(ids))
    rows = db.q(f"SELECT id,title,artist,name,path,size,ext,q_score,q_label,q_advice,"
                f"dup_best,dup_key,exact_key FROM songs WHERE id IN ({ph})", ids)
    by_id = {r["id"]: r for r in rows}
    items, errors = [], []
    for i in ids:
        r = by_id.get(i)
        label = (r or {}).get("title") or f"id {i}"
        if not r:
            errors.append(f"id {i}：曲库中已不存在")
            continue
        if r["dup_best"]:
            errors.append(f"{label}：这是同曲组的最佳版本，禁止清理")
            continue
        if "可清理" not in (r["q_advice"] or ""):
            errors.append(f"{label}：未标记为可清理的低质副本，禁止清理")
            continue
        if not r["path"] or not _in_roots(r["path"], cfg):
            errors.append(f"{label}：路径不在音乐目录内，禁止清理")
            continue
        if (r["ext"] or "").lower() not in _AUDIO_EXT:
            errors.append(f"{label}：不是受支持的音频文件，禁止清理")
            continue
        if not os.path.isfile(r["path"]):
            errors.append(f"{label}：文件已不存在")
            continue
        keep = None
        if r["dup_key"]:
            keep = db.one("SELECT id,title,path,q_score FROM songs WHERE dup_key=? "
                          "AND dup_best=1 AND id<>? ORDER BY q_score DESC LIMIT 1",
                          (r["dup_key"], i))
        if not keep:
            errors.append(f"{label}：同曲组找不到可保留的最佳版本，禁止清理")
            continue
        items.append({"id": i, "title": r["title"] or r["name"] or "—",
                      "artist": r["artist"] or "", "path": r["path"],
                      "size": r["size"] or 0, "ext": r["ext"],
                      "q_score": r["q_score"], "q_label": r["q_label"],
                      "advice": r["q_advice"], "keep_title": keep["title"],
                      "keep_path": keep["path"]})
    if not items:
        return {"ok": False, "error": "没有可安全清理的文件", "detail": errors}
    return {"ok": True, "items": items, "ids": [x["id"] for x in items],
            "errors": errors, "count": len(items),
            "total_bytes": sum(x["size"] for x in items),
            "token": _trash_token([x["id"] for x in items])}


def _move_to_trash(path: str) -> tuple[bool, str, str]:
    """返回 (成功, 方式, 备注)。唯一成功判据：文件已离开原路径。

    send2trash 在 Windows 上会拼 `\\\\?\\` 长路径前缀；路径含正斜杠时它会抛
    FileNotFoundError，但文件其实已被送进回收站。所以不能靠「有没有异常」判断，
    必须调用后重新检查文件是否还在原处，否则曲库记录会与磁盘状态脱节。
    """
    note = ""
    if _send2trash:
        try:
            # 统一成反斜杠的绝对 Windows 路径，规避 send2trash 的长路径前缀 bug
            _send2trash(os.path.normpath(os.path.abspath(path)))
        except Exception as e:
            note = f"回收站接口提示：{e}"
        if not os.path.exists(path):
            return True, "回收站", note      # 文件已离开，即便接口报错也算成功
        if not note:
            note = "回收站调用后文件仍在原处"
    try:                                    # 降级：同盘移动，同样可还原，绝不 unlink
        os.makedirs(TRASH_FALLBACK_DIR, exist_ok=True)
        drive, tail = os.path.splitdrive(os.path.abspath(path))
        safe = drive.replace(":", "").replace("\\", "") + "_" + \
            re.sub(r"[^\w\u4e00-\u9fff.\-]", "_", tail.strip("\\/"))
        dst = os.path.join(TRASH_FALLBACK_DIR, safe)
        n = 1
        while os.path.exists(dst):
            dst = os.path.join(TRASH_FALLBACK_DIR, f"({n})_{safe}")
            n += 1
        shutil.move(path, dst)
        return True, "隔离目录", (note + "；" if note else "") + f"已移至 {dst}"
    except Exception as e:
        if not os.path.exists(path):        # 部分成功后兜底，避免误判为失败
            return True, "回收站", (note + "；" if note else "") + "文件已离开原路径"
        return False, "", (note + "；" if note else "") + f"隔离失败：{e}"


def _append_trash_log(rec: dict) -> None:
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        with open(TRASH_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        # 不阻断清除，但必须留痕，避免历史上那种「静默失败」再发生
        print(f"[quality] 清理日志写入失败: {e}", flush=True)


def trash_files(db, ids, token: str, cfg) -> dict:
    """执行清除：必须先经 preview_trash 且令牌匹配。"""
    ids = _clean_ids(ids)
    if not ids:
        return {"ok": False, "error": "尚未选择要清理的文件"}
    if len(ids) > TRASH_BATCH_MAX:
        return {"ok": False, "error": f"单批最多 {TRASH_BATCH_MAX} 首，请分批操作"}
    if not token or token != _trash_token(ids):
        return {"ok": False, "error": "确认令牌无效或已过期，请重新点「清除已选」预览"}
    pre = preview_trash(db, ids, cfg)
    if not pre.get("ok") or pre["ids"] != ids:
        return {"ok": False, "error": "待清理列表已变化，请重新预览确认",
                "detail": pre.get("errors") or pre.get("error")}

    done, failed = [], []
    for it in pre["items"]:
        ok, method, note = _move_to_trash(it["path"])
        # 不变量：文件离开原路径 ⇔ 曲库记录删除。只要文件确实不在了就必须同步，
        # 否则界面会一直显示已清掉的歌（本次 bug 的根因）。
        if not ok and not os.path.exists(it["path"]):
            ok, method, note = True, "回收站", (note + "；" if note else "") + "文件已离开原路径，已同步曲库"
        if not ok:
            failed.append({"id": it["id"], "title": it["title"], "reason": note})
            continue
        with db.tx() as c:
            c.execute("DELETE FROM playlist_items WHERE song_id=?", (it["id"],))
            c.execute("DELETE FROM lyrics WHERE song_path=?", (it["path"],))
            c.execute("DELETE FROM songs WHERE id=?", (it["id"],))
        try:                                    # 封面缓存属于本项目，可安全移除
            cover = os.path.join(_DATA_DIR, "covers", f"{it['id']}.jpg")
            if os.path.isfile(cover):
                os.remove(cover)
        except Exception:
            pass
        _append_trash_log({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "id": it["id"],
                           "title": it["title"], "path": it["path"], "size": it["size"],
                           "advice": it["advice"], "keep_path": it["keep_path"],
                           "method": method, "note": note})
        done.append({"id": it["id"], "title": it["title"], "path": it["path"],
                     "size": it["size"], "method": method, "note": note})
    if done:
        refresh(db)                             # 重算评分与同曲组（不触发解码）
    freed = sum(d["size"] for d in done)
    note = (f"已清除 {len(done)} 首，释放 {freed / 1048576:.0f} MB；可在回收站还原"
            if done else "没有文件被移走，曲库未变更")
    if failed:
        note += f"；{len(failed)} 首未能移动（文件仍在原处）"
    return {"ok": True, "done": done, "failed": failed, "count": len(done),
            "freed_bytes": freed, "log": TRASH_LOG, "note": note}


def sync_missing(db) -> dict:
    """自愈：清掉「文件已不在磁盘上」的曲库残留记录（只动数据库，不碰任何文件）。

    用于修复历史不一致，或用户手动在资源管理器里移走歌曲后的状态同步。
    """
    rows = db.q("SELECT id,path,title FROM songs")
    gone = [r for r in rows if not os.path.isfile(r["path"])]
    if not gone:
        return {"ok": True, "removed": 0, "total": len(rows), "titles": []}
    with db.tx() as c:
        for r in gone:
            c.execute("DELETE FROM playlist_items WHERE song_id=?", (r["id"],))
            c.execute("DELETE FROM lyrics WHERE song_path=?", (r["path"],))
            c.execute("DELETE FROM songs WHERE id=?", (r["id"],))
    refresh(db)                             # 重算评分 / 同曲组，避免重复组里留幽灵
    return {"ok": True, "removed": len(gone), "total": len(rows),
            "titles": [(r["title"] or "") for r in gone][:20]}


def trash_log(limit: int = 50) -> dict:
    if not os.path.isfile(TRASH_LOG):
        return {"items": [], "total": 0, "log": TRASH_LOG}
    out = []
    try:
        with open(TRASH_LOG, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return {"items": list(reversed(out[-max(1, int(limit)):])), "total": len(out),
            "log": TRASH_LOG}
