"""氛围分类：11 维音频特征 + 文本线索（标题/歌手/专辑/文件夹名）-> 8 个氛围歌单。
规则打分：可解释（每首记录理由）、无需训练、零额外依赖。
文本线索权重高于纯听音，因为「纯音乐」「抖音DJ版」这类目录/文件名极其可靠。
"""
from __future__ import annotations

import re

MOODS = [
    # key, 名称, emoji, 说明
    ("hype",     "燃 · 劲爆",   "🔥", "高能量强节拍，运动 / 开车提神"),
    ("groove",   "律动 · 说唱", "🎤", "BPM 中低但咬字密、低频重的 rap / R&B / 电音律动"),
    ("rock",     "摇滚 · 硬核", "🤘", "失真粗糙、冲击力强"),
    ("pop",      "流行 · 主流", "🎧", "人声清晰的常规流行，不确定时的归位歌单"),
    ("cheer",    "轻快 · 愉悦", "🍃", "明亮顺滑，通勤日常背景"),
    ("ballad",   "深情 · 慢歌", "🌙", "人声主导的抒情慢板"),
    ("sad",      "致郁 · 伤感", "🌧", "暗色、低亮度、情绪下沉"),
    ("chill",    "治愈 · 纯音乐", "🎹", "原声器乐、无攻击性，学习 / 睡眠"),
    ("ambient",  "氛围 · 电子", "🌌", "铺底质感、空间感强的 instrumental / electronic"),
]
MOOD_MAP = {k: (n, e, d) for k, n, e, d in MOODS}
MOOD_KEYS = [k for k, *_ in MOODS]

# 文件名/标签/目录名 线索 -> 氛围（命中加权）
HINTS: list[tuple[str, str, float]] = [
    ("hype",    r"(dj|酒吧|串烧|remix|drop|bounce|clap|燃|嗨|战歌|健身|运动|workout|car|"
                r"edm|big ?room|hardstyle|phonk|trap ?dj|电音|嗨曲|劲爆|慢摇)"),
    ("groove",  r"(rap|hip ?hop|说唱|嘻哈|freestyle|trap|drill|flow|punchline|中国新说唱|"
                r"r&b|rb|soul|funk|律动|lil|ty|nop|谢帝|马思唯|姜云升)"),
    ("rock",    r"(rock|punk|metal|摇滚|金属|朋克|hardcore|screamo|garage|nu ?metal|硬核|"
                r"linkin|imagine ?dragons|nightwish|beyond|五月天)"),
    ("pop",     r"(流行|pop|单曲|抖音|热歌|新歌|榜单|vlog|华语|网络歌手|翻唱|cover|"
                r"周杰伦|林俊杰|李荣浩|薛之谦|毛不易|邓紫棋|陈奕迅|Taylor|Adele|Ed ?Sheeran)"),
    ("ballad",  r"(情歌|慢歌|抒情|ballad|钢琴版|对唱|恋|思念|告白|爱而不得|后来|可惜没有如果)"),
    ("sad",     r"(伤感|悲伤|心碎|失恋|催泪|致郁|孤独|离别|遗憾|痛|哭|sad|cry|tears|"
                r"emotional|goodbye|forget)"),
    ("chill",   r"(纯音乐|纯音|钢琴|古琴|古筝|小提琴|大提琴|弦乐|classical|orchestra|轻音乐|"
                r"lullaby|sleep|study|relax|healing|治愈|禅|bandari|ayasa|久石让|坂本龙一|"
                r"ludovico|yiruma|clayderman|the ?piano|music ?box|八音盒)"),
    ("ambient", r"(lo-?fi|chillwave|synthwave|vaporwave|house|techno|trance|deep ?house|"
                r"instrumental|背景|氛围|空灵|reverb|soundscape|downtempo|game ?ost|ost|"
                r"cinematic|score|theme)"),
    ("cheer",   r"(甜|可爱|欢快|开心|summer|happy|sunny|阳光|元气|candy|bubblegum|ukulele|"
                r"count ?on ?me|小星星|儿歌)"),
]
HINT_RE = [(k, re.compile(p, re.I)) for k, p in HINTS]

INSTRUMENTAL_RE = re.compile(r"(纯音乐|纯音|伴奏|instr|instrumental|无歌词|piano ?ver|钢琴版|"
                             r"轻音乐|beat ?only|karaoke|ost|原声|主题曲音乐)", re.I)
VOCAL_RE = re.compile(r"(live|现场|对唱|合唱|feat|翻唱|cover|人声|vocal)", re.I)


def _text_hints(text: str, folder: str) -> tuple[dict[str, float], bool]:
    """标题/歌手/专辑 权重 1.35；目录名权重 1.05。
    返回 (线索权重, 是否强制器乐)。目录叫「纯音乐」是用户自己下的定论，作硬门控。
    """
    hints: dict[str, float] = {}
    for mood, rx in HINT_RE:
        if rx.search(text):
            hints[mood] = hints.get(mood, 0.0) + 1.35
        if folder and rx.search(folder):
            hints[mood] = hints.get(mood, 0.0) + 1.05
    force_instr = bool(INSTRUMENTAL_RE.search(folder or ""))
    if INSTRUMENTAL_RE.search(text) or force_instr:
        hints["chill"] = hints.get("chill", 0.0) + 1.10
        hints["ambient"] = hints.get("ambient", 0.0) + 0.55
        hints["ballad"] = hints.get("ballad", 0.0) - 0.90
        hints["groove"] = hints.get("groove", 0.0) - 0.70
    return hints, force_instr


def _score(f: dict, hints: dict[str, float], force_instr: bool = False) -> dict[str, float]:
    e, d, a, v = f["energy"], f["danceability"], f["acoustic"], f["vocal"]
    val, br, ro, fl = f["valence"], f["brightness"], f["roughness"], f["flatness"]
    tp, to, dy = f["tempo"], f.get("tonality", 0.5), f.get("dynamic", 0.5)
    md = f.get("mod", 0.5)                      # 幅度调制规整度：器乐高、人声低
    # 原始未饱和值（归一值在响度战争素材上大面积 1.0，门控必须用原始值）
    v_raw = float(f.get("v_raw", 0.45))         # 实测：纯音乐 .2~.4 / 人声流行 .6~.9
    a_raw = float(f.get("a_raw", 0.6))          # 实测：器乐 .65+ / 人声 .55-
    s = {k: 0.0 for k in MOOD_KEYS}

    fast, mid, slow = tp >= 118, 82 <= tp < 118, 0 < tp < 82
    is_instr = v_raw < 0.42 and a_raw > 0.60    # 器乐硬判据
    is_vocal = v_raw > 0.52                     # 人声硬判据

    # 燃：能量 + 律动 + 快（人声炸曲也算，DJ 线索另加权）
    s["hype"] += e * 1.0 + d * 0.75 + (0.6 if fast else 0.1 if mid else -0.15) + ro * 0.3
    # 律动说唱：中低 BPM、人声清晰、能量中高、亮度偏低
    s["groove"] += (0.8 if mid else 0.3 if slow else 0.0) + v * 0.75 + e * 0.35 + (1 - br) * 0.35 - d * 0.15
    # 摇滚：宁缺毋滥。响度战争下高能量 DJ/神曲的 roughness 与真摇滚无法用音频区分
    #     （实测 rock 歌单里大量「野花香/沙漠骆驼/干饭人之歌」），因此要求文本线索背书。
    s["rock"] += ro * 1.3 + e * 0.45 + (0.4 if fast or mid else -0.2) + (1 - a) * 0.3 + (1 - to) * 0.6
    if hints.get("rock", 0) <= 0:
        s["rock"] -= 4.0
    elif ro < 0.5 or to > 0.5:
        s["rock"] -= 1.2
    # 流行：人声清晰 + 节拍正常（人声素材的归位歌单）
    s["pop"] += v * 0.75 + (1 - a) * 0.45 + (0.45 if mid or fast else 0.0) + to * 0.15 + (1 - md) * 0.5 + 0.35
    # 轻快：明亮 + 顺滑 + 中快 + 中等能量
    s["cheer"] += val * 0.9 + br * 0.5 + (0.5 if mid or fast else 0.0) + (1 - ro) * 0.45 - e * 0.2
    # 深情慢歌：人声强 + 慢 + 能量中低
    s["ballad"] += v * 0.9 + (0.75 if slow else 0.15 if mid else -0.2) + (1 - e) * 0.45 + to * 0.25
    # 伤感：暗 + 低能量 + 慢
    s["sad"] += (1 - val) * 0.9 + (1 - br) * 0.5 + (1 - e) * 0.35 + (0.4 if slow else 0.0) + to * 0.2
    # 治愈纯音乐：器乐判据 + 低粗糙 + 低能量 + 调制规整
    s["chill"] += a * 0.8 + to * 0.5 + (1 - ro) * 0.5 + (1 - e) * 0.7 + dy * 0.2 + md * 0.5 - v * 0.6 - (0.7 if fast else 0.0)
    # 氛围电子：平坦 + 非人声 + 中慢
    s["ambient"] += fl * 0.85 + (1 - v) * 0.7 + (1 - a) * 0.35 + (0.35 if mid or slow else 0.0) + br * 0.15

    # ---- 硬门控（覆盖软打分，防止归一饱和导致的串味）----
    if is_instr or force_instr:      # 器乐 / 目录名已声明纯音乐：锁死器乐类
        s["chill"] += 1.6
        s["ambient"] += 0.6
        for k in ("pop", "ballad", "groove", "rock", "sad", "cheer"):
            s[k] -= 2.2
        if tp >= 128:                # 快节奏器乐 -> 电子/燃，不回 chill
            s["chill"] -= 1.4
            s["hype"] += 0.5
            s["ambient"] += 0.4
    elif is_vocal:                   # 人声：赶出纯音乐/氛围
        s["chill"] -= 2.0
        s["ambient"] -= 1.4
        s["pop"] += 0.35
    else:                            # 中间地带：调制比裁决
        if md > 0.62:
            s["chill"] += 0.7
            s["ambient"] += 0.3
            s["pop"] -= 0.5
            s["ballad"] -= 0.4
        elif md < 0.28:
            s["pop"] += 0.5
            s["ballad"] += 0.3
            s["chill"] -= 0.7
            s["ambient"] -= 0.5

    for mood, w in hints.items():
        s[mood] += w
    return s


def classify(feat: dict, title: str = "", artist: str = "", album: str = "",
             folder: str = "") -> tuple[str, str, dict]:
    """返回 (mood_key, 判定理由, 各氛围得分)。"""
    text = " ".join(x for x in (title, artist, album) if x)
    hints, force_instr = _text_hints(text, folder)
    scores = _score(feat, hints, force_instr)
    ordered = sorted(scores.items(), key=lambda kv: -kv[1])
    key, top = ordered[0]
    second = ordered[1]

    parts = []
    if hints:
        strong = sorted(k for k, w in hints.items() if w > 0)
        if strong:
            parts.append("线索:" + "/".join(MOOD_MAP[k][0] for k in strong[:2]))
    parts.append(f"BPM {feat['tempo']:.0f}")
    parts.append(f"能量 {feat['energy']:.2f}")
    parts.append(f"人声 {feat['vocal']:.2f}")
    parts.append(f"器乐 {feat['acoustic']:.2f}")
    parts.append(f"调制 {feat.get('mod', 0):.2f}")
    reason = " · ".join(parts) + f" → {MOOD_MAP[key][0]}"
    if second[1] > top - 0.35:
        reason += f"（次选 {MOOD_MAP[second[0]][0]}）"
    return key, reason, {k: round(v, 3) for k, v in scores.items()}


def all_moods() -> list[dict]:
    return [{"key": k, "name": n, "emoji": e, "description": d, "sort": i}
            for i, (k, n, e, d) in enumerate(MOODS)]
