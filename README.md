# 氛围音乐播放器

本地歌曲自动按氛围分类进歌单 + 音质评判/自动去重 + 歌词全文搜索的极简播放器。
纯 Python 后端（stdlib + mutagen + numpy），无框架前端，**冷启动 ~1 秒**，扫描/分析全部在后台增量进行。

## 快速开始

```bat
双击「启动播放器.bat」
```

或手动：

```bat
pip install -r requirements.txt
python app.py                 # 打开 http://127.0.0.1:8760
python app.py --port 8888     # 换端口
python app.py --rescan        # 强制重扫目录
python app.py --no-browser    # 不自动开浏览器
```

默认扫描 `D:\方格`、`D:\纯音乐`、`D:\废歌`、`%USERPROFILE%\Music`（可在 ⚙ 设置里改，写入 `data/config.json`）。

## 为什么启动快

| 阶段 | 做法 | 耗时 |
|---|---|---|
| 进程启动 | 只 import mutagen/numpy（不 import librosa/scipy） | ~0.3s |
| 绑定端口 | 服务先起，**不等扫描** | ~0.1s |
| 首次扫描 | 只读文件标签（不解码音频），147 首实测 0.1s | 秒级 |
| 音频分析 | 后台线程池，边分析边重建歌单，UI 随时可用 | 每首 ~0.4s |
| 播放 | HTTP Range 直读原文件；ape/wma 等浏览器不支持的格式 ffmpeg 即时转 mp3 并永久缓存 | 首包 <0.2s |

## 氛围分类（9 个自动歌单）

🔥 燃·劲爆 ｜ 🎤 律动·说唱 ｜ 🤘 摇滚·硬核 ｜ 🎧 流行·主流 ｜ 🍃 轻快·愉悦 ｜ 🌙 深情·慢歌 ｜ 🌧 致郁·伤感 ｜ 🎹 治愈·纯音乐 ｜ 🌌 氛围·电子

每首歌提取 12 维特征 + 文本线索，规则打分并**记录判定理由**（点开右侧「分析」页可见）：

- **音频特征**：BPM（onset 自相关 + 倍频纠错）、能量、律动度、原声度、明亮度、粗糙度、频谱平坦度、谐波集中度、人声存在感、幅度调制比（器乐调制规整 vs 人声调制分散）
- **文本线索**：标题/歌手/专辑关键词（权重 1.35）+ **所在目录名**（权重 1.05，如整个 `D:\纯音乐` 目录会硬锁器乐类）
- **硬门控**：摇滚必须有文本线索背书（响度战争下高能量 DJ 曲的 roughness 与真摇滚在音频上无法区分，实测「野花香/沙漠骆驼」都会误入）；快节奏器乐走电子/燃而非治愈
- 阈值不是拍的：对三个曲库目录各抽样 10 首，按实测分布（p10/p35/med/p75）标定了每个特征的归一区间；`core/features.py` 顶部注释记录了实测分布
- 改算法后把 `library.py` 的 `FEAT_VER` +1 全库重算；**只改分类规则**时调 `POST /api/reclassify`，用已存特征 0.1s 重排，不用重新解码

实测（998 首）与目录弱标签的一致性：`纯音乐` 目录 98% 进器乐歌单；`rock` 歌单 5 首全部为 Beyond 等真摇滚；`hype` 中 DJ 版曲目 34/110。

## 音质评判 · 自动去重

顶栏切到「音质 / 去重」。默认**只做识别、评分与清单，不改动任何文件**；需要清理时由用户逐项勾选并二次确认，文件移入回收站，可还原。

- **L1 元数据评分（零解码，全库 <0.2s）**：按容器（flac/ape/wav/wv/tta 为无损，mp3/m4a/ogg 为有损）、有效码率（`size×8/duration`）、采样率给出 0~100 分与档位：真无损 85~100 / 高码率有损 62~75 / 有损 48~62 / 低码率 <48 / 疑似假无损 28~45。判定依据写入 `q_reason`，鼠标悬停可看。
- **L2 频谱深度检测（按需）**：点「🔬 深度检测」，ffmpeg 解码 9 秒 / 44.1kHz 估计高频截止。本机实测真无损截止达 21750Hz，mp3 多在 15750~20750Hz；标称无损但截止 <20000Hz 判「疑似假无损」（识别 mp3 转 flac/wav）。结果缓存入库只跑一次；原始采样率 <44.1kHz 的跳过，避免 Nyquist 误报。
- **去重两层**：
  - 同曲不同版本：规范化歌名 + 歌手（只剔除音质/来源标记，保留 Live/Remix/DJ 等版本词）+ 时长容差（±6s 或 ±10%）聚类，组内按分数标「保留（最佳版本）」/「可清理低质副本」。
  - 完全相同文件：size 相同的组内做头尾指纹，标「完全相同文件，可清理其一」。
- **待换无损清单**：按同曲组聚合，若该组最高分 <80（没有无损版本），取组内最佳为代表，按分数从低到高排序——即优先要去换无损的歌。
- **导出**：「⬇ 导出清单」输出 CSV（UTF-8 BOM，Excel 直接打开），含歌名/歌手/格式/码率/采样率/评分/建议/完整路径。每行可播放试听或一键复制路径。

### 安全清除（移入回收站）

切到「重复组」→ 勾选低质副本（或点「☑ 选中可清理」一次选满 10 首）→「🗑 清除已选」→ 弹窗逐项列出**完整路径、体积、同曲组保留的最佳版本**→ 确认后执行。

- 底层调用 `send2trash` 把文件送进 **Windows 回收站**，随时可还原；绝不使用 `os.remove` / `del`。若该库不可用，降级为移动到项目内 `data/trash/` 隔离目录，日志会记录新位置。
- 服务端五道校验：① 仅允许 `q_advice` 标记为「可清理」的同曲组非最佳版本，每组至少保留一首；② 路径必须 `realpath` 落在 `scan_roots` 内；③ 扩展名必须是受支持的音频格式且文件真实存在；④ 单批 ≤10 首；⑤ 必须先调预览接口拿到服务端签发的确认令牌，令牌与 ids 绑定、重启失效。
- 清除后同步删除 `songs` / `playlist_items` / `lyrics` 记录与封面缓存，并重算评分与重复组；歌单不会留下空条目。
- 每次操作追加写入 `data/trash_log.jsonl`（时间、原路径、体积、保留版本、去向），「🧾 清理记录」可查看，便于从回收站找回。
- **状态不变量**：文件离开原路径 ⇔ 曲库记录删除。`send2trash` 在 Windows 上会拼 `\\?\` 长路径前缀，路径含正斜杠时可能抛异常但文件其实已进回收站；因此调用前统一 `normpath` 规范化，调用后**只以「文件是否仍在原处」判定成功**，接口报错但文件已移走时照样同步曲库。
- 「⟳ 同步状态」用于自愈：扫描曲库中「文件已不在磁盘上」的残留记录并清除（只动数据库，不碰文件），对应 `POST /api/quality/sync`。历史状态不一致或手动在资源管理器移走歌曲后可点它。

> 边界：评分是规则打分而非听感评测；母带本身低通的老录音可能被标「疑似假无损」，请结合 `q_reason` 中的截止频率人工复核。清除前请核对弹窗里的完整路径；回收站会占用磁盘空间，需彻底释放请自行清空回收站。

## 歌词

- 存储：`data/lyrics/<song_id>.json`（结构化时间轴）+ SQLite 全文索引
- 来源：内嵌歌词（ID3 USLT / Vorbis / MP4 ©lyr）自动抽取 → 外部 `.lrc/.json/.txt` 批量导入（⚙ 里填目录）→ 手动粘贴（自动解析时间轴，无时间轴按曲长均分）
- 搜索：顶栏切到「搜歌词」，按歌词内容全文检索，点结果直接定位到命中句播放

## 快捷键

`空格` 播放/暂停 ｜ `←/→` 快退/快进 5s ｜ `↑/↓` 上一首/下一首 ｜ `L` 歌词面板 ｜ `/` 搜索

## API 一览

```
GET  /api/state                 扫描/分析进度、曲库计数
GET  /api/playlists             歌单（含自动歌单与用户歌单）
GET  /api/songs?playlist=&q=&order=&limit=&offset=
GET  /api/song/<id>             单曲详情（含 12 维特征）
POST /api/song/<id>/reparse     单首重新解析标签
GET  /api/lyric/<id>            歌词 JSON
POST /api/lyric/<id>            保存歌词 {lrc: "..."}
GET  /api/search?q=关键词       歌词全文搜索（返回命中行与时间点）
POST /api/scan                  重新扫描 + 增量分析
POST /api/reclassify            只改了分类规则时：用已存特征重排全库（0.1s，不解码）
POST /api/import                导入歌词目录 {dir, remember}
GET  /api/config · POST /api/config
POST /api/playlist              条件歌单 {name, where, order, limit}
GET  /api/quality/overview       音质概览（无损/待换/重复组/疑似假无损/可释放空间）
GET  /api/quality/list?filter=   清单 upgrade|dup|fake|lossy|all
GET  /api/quality/duplicates     重复组（含成员与可释放体积）
GET  /api/quality/state          频谱深度检测进度
GET  /api/quality/export?kind=  导出 CSV：upgrade|dup|all
POST /api/quality/refresh        零解码重算评分与去重
POST /api/quality/deep           后台频谱检测 {limit, workers}
POST /api/quality/trash/preview  清除预览 {ids} -> 清单 + 确认令牌（不碰文件）
POST /api/quality/trash          执行清除 {ids, token} -> 移入回收站
GET  /api/quality/trash/log      清理记录（含原路径与保留版本）
POST /api/quality/sync           状态自愈：移除文件已不在磁盘上的残留记录
GET  /audio/<id>                Range 流（必要时 ffmpeg 转码缓存）
GET  /cover/<id>                内嵌封面
```

## 目录结构

```
music-player/
├── app.py                  入口（先绑端口，后台扫描/分析）
├── core/
│   ├── config.py           配置与路径
│   ├── db.py               SQLite（WAL、线程局部连接、列迁移）
│   ├── parse.py            标签 + 文件名智能解析 + 内嵌歌词/封面
│   ├── features.py         ffmpeg 解码 -> numpy STFT -> 12 维特征
│   ├── moods.py            规则分类器（特征 + 文本/目录线索 -> 9 氛围）
│   ├── quality.py          音质评分 / 频谱截止 / 判重 / CSV 清单 / 安全清除
│   ├── lyrics.py           LRC/JSON 解析、存储、全文搜索、批量导入
│   ├── library.py          扫描 / 后台分析 / 自动重建歌单
│   └── server.py           HTTP：Range 流、转码缓存、API
├── web/                    前端（index.html + style.css + app.js）
├── data/                   music.db · lyrics/ · covers/ · cache/ · config.json
└── requirements.txt
```

## 已知边界

- 分类是规则打分，不是机器学习；「流行·主流」是兜底歌单，边界曲目可能归错，但每首的理由可见、可自己调 `moods.py` 权重
- BPM 对无打击乐的纯音乐可能给 0（不影响分类，走器乐分支）
- 转码缓存按文件路径+大小哈希，改名会重转一次
- 数据都在 `data/` 下，删掉即重置；`data/config.json` 可手改
