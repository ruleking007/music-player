"""歌曲解析：优先用内嵌标签，缺失时用文件名智能解析（多策略打分）。
同时抽取内嵌歌词（ID3 USLT / Vorbis LYRICS / MP4 ©lyr），提高歌词命中率。
纯 stdlib + mutagen，导入耗时 < 0.2s，保证启动快。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from mutagen import File as MFile

# ---------------------------------------------------------------- 常量
_NOISE = re.compile(r"\[(mqms2?|mq|320k|128k|192k|256k|flac|ape|mv|vip|付费|试听|hq|sq|hi-?res|"
                    r"dolby|atmos|空间音频|无损|高音质|伴奏|动态|官方)?[^\]]*\]", re.I)
_BRACKET_NOISE = re.compile(r"[（(](?:mqms2?|320k|128k|flac|ape|vip|试听|伴奏|无损|高清|live|现场版?|"
                            r"抖音|网易云|qq音乐|铃声|clip|片段|snippet)[^）)]*[）)]", re.I)
_TAG_STRIP = re.compile(r"\s*[-–—]?\s*(?:单曲|专辑|EP|Live\s*版?|MV|伴奏|纯享版?|翻唱|"
                        r"Official\s*(?:Audio|Music\s*Video|Lyrics?(?:\s*Video)?))\s*$", re.I)
_DOUYIN = re.compile(r"[（(【\[]?\s*(?:抖音|抖音版|DJ版|抖音DJ版|慢摇|弹舌|加速版?|超燃|热门|热评|vip|"
                     r"高潮版|remix|mix|60秒|30秒|片段|live|现场)\s*[）)】\]]?", re.I)

# 常见“歌手 - 歌名”分隔符，按可信度排序
_SEPARATORS = [" - ", " — ", " – ", "-", "&", "＆", "、", ",", "，", "+", " feat. ", " feat ",
               " ft. ", " ft ", " × ", " x ", "/", " | "]

KNOWN_TAG_ARTISTS = None  # 预留


@dataclass
class Parsed:
    path: str
    filename: str
    title: str = ""
    artist: str = ""
    album: str = ""
    duration: float = 0.0
    bitrate: int = 0
    samplerate: int = 0
    size: int = 0
    ext: str = ""
    has_meta: bool = False          # 是否来自内嵌标签
    embedded_lyric: str = ""        # 内嵌歌词原文
    cover: bytes = b""
    source: str = ""                # tag / filename / raw
    confidence: float = 0.0
    extra: dict = field(default_factory=dict)

    def as_row(self) -> dict:
        return {
            "path": self.path, "name": self.filename, "title": self.title, "artist": self.artist,
            "album": self.album, "ext": self.ext, "size": self.size, "duration": self.duration,
            "bitrate": self.bitrate, "samplerate": self.samplerate, "has_meta": int(self.has_meta),
        }


# ---------------------------------------------------------------- 标签读取
def _first(v):
    if isinstance(v, (list, tuple)):
        return str(v[0]) if v else ""
    return str(v) if v is not None else ""


def _read_embedded_lyric(audio) -> str:
    """尽力抽取内嵌歌词文本。"""
    try:
        if audio is None:
            return ""
        tags = getattr(audio, "tags", None)
        if tags is None:
            return ""
        # Vorbis / APE / ASF
        for key in ("LYRICS", "lyrics", "Lyrics", "SCRIPT", "UNSYNCLYRICS"):
            val = tags.get(key)
            if val:
                txt = _first(val)
                if txt.strip():
                    return txt
        # ID3 USLT / SYLT
        if hasattr(tags, "getall"):
            best = ""
            for k in list(tags.keys()):
                if k.startswith("USLT") or k.startswith("SYLT"):
                    fr = tags.get(k)
                    txt = getattr(fr, "text", None) or _first(fr)
                    if txt and len(txt) > len(best):
                        best = txt
            if best.strip():
                return best
        # MP4 ©lyr
        for key in ("\xa9lyr", "©lyr"):
            val = tags.get(key)
            if val:
                txt = _first(val)
                if txt.strip():
                    return txt
    except Exception:
        pass
    return ""


def _read_cover(audio) -> bytes:
    try:
        pics = getattr(audio, "pictures", None)
        if pics:
            return pics[0].data or b""
        tags = getattr(audio, "tags", None)
        if tags is not None and hasattr(tags, "getall"):
            for apic in tags.getall("APIC", []):
                data = getattr(apic, "data", None)
                if data:
                    return data
        if tags is not None:
            cov = tags.get("covr") or tags.get("©art")
            if cov:
                b = cov[0] if isinstance(cov, (list, tuple)) else cov
                if isinstance(b, bytes):
                    return b
                if hasattr(b, "data"):
                    return b.data or b""
    except Exception:
        pass
    return b""


def read_tags(path: str) -> tuple[dict, str, bytes, dict]:
    """返回 (title/artist/album dict, 内嵌歌词, 封面字节, 技术参数)。"""
    out = {"title": "", "artist": "", "album": ""}
    tech = {"duration": 0.0, "bitrate": 0, "samplerate": 0}
    lyric, cover = "", b""
    try:
        audio = MFile(path, easy=False)
        if audio is None:
            audio = MFile(path, easy=True)
        if audio is None:
            return out, lyric, cover, tech
        tags = getattr(audio, "tags", None)
        if tags is not None:
            # 通用 mutagen easy/ID3 键
            def g(*keys):
                for k in keys:
                    try:
                        v = tags.get(k)
                    except Exception:
                        v = None
                    if v:
                        s = _first(v).strip()
                        if s:
                            return s
                return ""
            out["title"] = g("title", "TIT2", "©nam", "TITLE")
            out["artist"] = g("artist", "TPE1", "©ART", "ARTIST", "albumartist", "TPE2")
            out["album"] = g("album", "TALB", "©alb", "ALBUM")
        lyric = _read_embedded_lyric(audio)
        cover = _read_cover(audio)
        info = getattr(audio, "info", None)
        if info is not None:
            tech["duration"] = float(getattr(info, "length", 0) or 0)
            tech["bitrate"] = int(getattr(info, "bitrate", 0) or 0)
            tech["samplerate"] = int(getattr(info, "sample_rate", 0) or 0)
            if not out["title"]:
                out["title"] = _first(getattr(info, "title", "")).strip()
    except Exception:
        pass
    return out, lyric, cover, tech


# ---------------------------------------------------------------- 文件名解析
def clean_name(stem: str) -> str:
    s = stem
    s = _NOISE.sub(" ", s)
    s = _BRACKET_NOISE.sub(" ", s)
    s = _DOUYIN.sub(" ", s)
    s = _TAG_STRIP.sub("", s)
    s = re.sub(r"\b\d{3,4}k\b", " ", s, flags=re.I)
    s = re.sub(r"^\s*\d{1,3}\s*[._、-]\s*", "", s)      # 序号前缀 "01. "
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip(" -–—.、,")


def _looks_like_artist(tok: str) -> float:
    """判断 token 像不像歌手名（返回 0~1）。"""
    t = tok.strip()
    if not t or len(t) > 60:
        return 0.0
    score = 0.5
    if re.search(r"[\u4e00-\u9fff]", t):
        score += 0.15
    if re.search(r"[A-Za-z]", t):
        score += 0.1
    # 纯数字/编号 或 明显是歌名特征
    if re.fullmatch(r"[\d\W_]+", t):
        score -= 0.4
    if re.search(r"(Official|Audio|Video|Lyric|MV|单曲|专辑)", t, re.I):
        score -= 0.3
    return max(0.0, min(1.0, score))


def parse_filename(stem: str) -> tuple[str, str, float]:
    """从文件名解析 (title, artist, confidence)。"""
    s = clean_name(stem)
    if not s:
        s = stem.strip()
    best = ("", "", 0.0)

    for sep in _SEPARATORS:
        if sep not in s:
            continue
        parts = [p.strip() for p in s.split(sep) if p.strip()]
        if len(parts) < 2:
            continue
        # 组合 feat./& 情况：第一段是 artist，其余是 title
        a, t = parts[0], " ".join(parts[1:])
        if sep in (" - ", " — ", " – ", "-"):
            # 判断哪边更像歌手：通常 "歌手 - 歌名"
            if _looks_like_artist(a) >= _looks_like_artist(t) or len(a) <= len(t):
                pass
            else:
                a, t = t, a
        conf = 0.62
        if sep in (" - ", " — ", " – "):
            conf = 0.82
        elif sep in ("&", "＆", "、"):
            conf = 0.55
        if len(a) > 40 or len(t) > 80:
            conf -= 0.2
        if conf > best[2]:
            best = (t, a, conf)

    if best[2] <= 0:
        # 没有分隔符：整体当 title
        return s, "", 0.3
    return best


# ---------------------------------------------------------------- 顶层
def parse_song(path: str) -> Parsed:
    p = os.path.normpath(path)
    stem = os.path.splitext(os.path.basename(p))[0]
    ext = os.path.splitext(p)[1].lower()
    try:
        st = os.stat(p)
        size = st.st_size
        mtime = st.st_mtime
    except OSError:
        size, mtime = 0, 0.0

    parsed = Parsed(path=p, filename=os.path.basename(p), ext=ext, size=size,
                    extra={"mtime": mtime})

    tags, lyric, cover, tech = read_tags(p)
    parsed.embedded_lyric = lyric
    parsed.cover = cover
    parsed.duration = tech["duration"]
    parsed.bitrate = tech["bitrate"]
    parsed.samplerate = tech["samplerate"]

    ft, fa, fl = (tags.get("title") or "").strip(), (tags.get("artist") or "").strip(), (tags.get("album") or "").strip()
    if ft and ft.lower() not in ("unknown", "未知"):
        parsed.title, parsed.artist, parsed.album = ft, fa, fl
        parsed.has_meta = True
        parsed.source = "tag"
        parsed.confidence = 0.95
    else:
        t, a, conf = parse_filename(stem)
        parsed.title, parsed.artist = t, a
        parsed.album = fl
        parsed.source = "filename"
        parsed.confidence = conf
        if not parsed.title:
            parsed.title, parsed.source, parsed.confidence = stem, "raw", 0.2

    if not parsed.title:
        parsed.title = stem
    return parsed
