"""配置：扫描目录、端口、数据路径。首次运行自动生成 data/config.json。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent          # music-player/
DATA_DIR = APP_DIR / "data"
LYRICS_DIR = DATA_DIR / "lyrics"
COVER_DIR = DATA_DIR / "covers"
CACHE_DIR = DATA_DIR / "cache"
DB_PATH = DATA_DIR / "music.db"
CONFIG_PATH = DATA_DIR / "config.json"
WEB_DIR = APP_DIR / "web"

AUDIO_EXTS = {
    ".mp3", ".flac", ".wav", ".ogg", ".oga", ".m4a", ".mp4", ".aac",
    ".ape", ".wma", ".aiff", ".aif", ".opus", ".wv", ".tta", ".dsf", ".dsd",
}
# 浏览器 <audio> 能直接播的格式，其余走 ffmpeg 即时转码缓存
BROWSER_NATIVE = {".mp3", ".flac", ".wav", ".ogg", ".oga", ".m4a", ".mp4", ".aac", ".opus", ".aiff", ".aif"}

_DEFAULT_ROOTS = [
    r"D:\方格",
    r"D:\纯音乐",
    r"D:\废歌",
    str(Path.home() / "Music"),
]

DEFAULTS = {
    "scan_roots": [p for p in _DEFAULT_ROOTS if os.path.isdir(p)] or [str(Path.home() / "Music")],
    "exclude_dirs": ["$RECYCLE.BIN", "System Volume Information", "node_modules", "ue",
                     "Delta Force", "miHoYo Launcher", "元气壁纸缓存", "Microsoft VS Code",
                     "Program Files", "WindowsApps"],
    "exclude_roots": [r"D:\ue"],
    "port": 8760,
    "host": "127.0.0.1",
    "analyze_seconds": 100,          # 每首歌截取多少秒音频做特征
    "analyze_workers": 2,
    "lyrics_import_dirs": [],        # 外部 .lrc/.json 歌词库目录，可手动添加
    "open_browser": True,
    "max_playlist_songs": 400,
}


def load() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for d in (LYRICS_DIR, COVER_DIR, CACHE_DIR):
        d.mkdir(parents=True, exist_ok=True)
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text("utf-8"))
            if isinstance(user, dict):
                cfg.update({k: v for k, v in user.items() if k in DEFAULTS})
        except Exception:
            pass
    else:
        save(cfg)
    cfg["scan_roots"] = [r for r in cfg["scan_roots"] if os.path.isdir(r)] or cfg["scan_roots"]
    return cfg


def save(cfg: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")


def fix_stdout() -> None:
    """Windows 控制台默认 gbk，中文歌名会炸。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass
