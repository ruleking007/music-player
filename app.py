"""氛围音乐播放器 · 入口
用法：
  python app.py              启动服务（秒开），后台扫描+分析
  python app.py --no-browser 不自动开浏览器
  python app.py --port 8888  换端口
  python app.py --rescan     启动时强制重新扫描
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
import webbrowser

from core import config, library, quality, server
from core.config import fix_stdout

fix_stdout()


def main() -> int:
    ap = argparse.ArgumentParser(description="本地氛围音乐播放器")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--rescan", action="store_true")
    ap.add_argument("--workers", type=int, default=None)
    args = ap.parse_args()

    t0 = time.time()
    ctx = server.Ctx()
    cfg = ctx.cfg
    if args.port:
        cfg["port"] = args.port
    if args.workers:
        cfg["analyze_workers"] = args.workers

    # 库为空时先做一次“只读标签”的快速登记（不阻塞端口绑定太久）
    n = ctx.db.one("SELECT COUNT(*) n FROM songs")["n"]
    boot = {"empty": n == 0}

    srv, _ = server.serve(ctx)
    url = f"http://{cfg['host']}:{cfg['port']}/"
    print(f"\n  ♪ 氛围音乐播放器已就绪  {url}")
    print(f"    启动耗时 {time.time()-t0:.2f}s · 曲库 {n} 首 · 扫描目录 {len(cfg['scan_roots'])} 个")
    print(f"    ffmpeg: {'可用（全格式播放）' if ctx.has_ffmpeg else '缺失（仅浏览器原生格式）'}")
    print("    Ctrl+C 退出\n")

    def boot_work():
        if boot["empty"] or args.rescan or n == 0:
            library.scan(ctx.db, cfg)
            library.rebuild_playlists(ctx.db, cfg)
        library.analyze_pending(ctx.db, cfg)
        quality.refresh(ctx.db)          # 零解码，秒级完成评分与去重

    threading.Thread(target=boot_work, name="boot", daemon=True).start()

    if cfg.get("open_browser", True) and not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  已退出。数据保存在 data/music.db，下次启动直接可用。")
        srv.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
