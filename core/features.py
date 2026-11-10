"""音频特征提取：ffmpeg 解码为单声道 PCM -> numpy/scipy 计算。
不依赖 librosa（导入要 6~11s），本模块冷启动 < 1s。

输出 9 个归一化特征，供 moods.classify 判定氛围：
  tempo        BPM
  energy       响度/力度
  danceability 节拍规整度 + 中低频冲击
  acoustic     原声/器乐倾向（低噪声、高谐波）
  valence      明亮度/情绪正向（频谱倾斜 + 谐波集中度）
  brightness   频谱质心
  roughness    粗糙度/失真（高频能量 + 过零率）
  flatness     频谱平坦度（越高越像噪声）
  vocal        人声存在感（中频带谐波能量占比）
"""
from __future__ import annotations

import os
import shutil
import subprocess
import numpy as np

SR = 22050
FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")
_HAS_FFMPEG = shutil.which(FFMPEG) is not None


def decode(path: str, seconds: float = 100.0, sr: int = SR) -> np.ndarray:
    """解码为 float32 单声道。优先 ffmpeg（支持全部格式），回退 soundfile。"""
    if _HAS_FFMPEG:
        cmd = [FFMPEG, "-v", "error", "-nostdin", "-ss", "0", "-t", str(int(seconds)),
               "-i", path, "-f", "f32le", "-acodec", "pcm_f32le", "-ac", "1", "-ar", str(sr), "-"]
        try:
            raw = subprocess.run(cmd, capture_output=True, timeout=90).stdout
            if len(raw) > sr * 2:                      # 至少 0.5 秒
                return np.frombuffer(raw, dtype=np.float32).copy()
        except Exception:
            pass
    import soundfile as sf
    y, r = sf.read(path, dtype="float32", always_2d=False)
    if y.ndim > 1:
        y = y.mean(axis=1)
    n = int(seconds * r)
    return np.ascontiguousarray(y[:n])


# ------------------------------------------------------------------ 工具
def _norm(x: float, lo: float, hi: float) -> float:
    if hi <= lo:
        return 0.0
    return float(np.clip((x - lo) / (hi - lo), 0.0, 1.0))


def _stft(y: np.ndarray, nperseg: int = 2048, hop: int = None):
    """numpy 手写 STFT（避免 scipy/librosa 依赖，冷启动更快）。"""
    hop = hop or nperseg // 2
    if y.size < nperseg:
        y = np.pad(y, (0, nperseg - y.size))
    win = np.hanning(nperseg).astype(np.float32)
    n_frames = 1 + (y.size - nperseg) // hop
    idx = np.arange(nperseg)[None, :] + hop * np.arange(n_frames)[:, None]
    frames = y[idx] * win
    spec = np.fft.rfft(frames, axis=1)
    freqs = np.fft.rfftfreq(nperseg, 1.0 / SR)
    times = (np.arange(n_frames) * hop + nperseg / 2) / SR
    # 统一为 (freqs, frames)，与下游代码一致
    return freqs, times, (np.abs(spec).T + 1e-10).astype(np.float32)


def _spec(y: np.ndarray, nperseg: int = 2048):
    return _stft(y, nperseg)


def _onset_env(y: np.ndarray) -> np.ndarray:
    """频谱差分 onset 包络（比整段 librosa 快很多）。"""
    f, t, S = _spec(y, 1024)
    d = np.diff(S, axis=1)
    d[d < 0] = 0
    env = d.sum(axis=0)
    env = np.concatenate([[env[0]], env])
    if env.max() > 0:
        env = env / env.max()
    return env.astype(np.float32)


def _tempo(env: np.ndarray, hop: float) -> tuple[float, float]:
    """自相关求 BPM + 节拍规整度，带倍频纠错（偏好较快的真实拍）。"""
    fps = 1.0 / hop
    lo, hi = int(fps * 60 / 200.0), int(fps * 60 / 58.0)   # 58~200 BPM
    if env.size < hi * 3:
        return 0.0, 0.0
    e = env - env.mean()
    ac = np.correlate(e, e, "full")[e.size - 1:]
    if ac[0] <= 0:
        return 0.0, 0.0
    ac = ac / ac[0]
    seg = ac[lo:hi]
    if seg.size == 0:
        return 0.0, 0.0
    k = int(np.argmax(seg))
    lag = lo + k
    peak = float(seg[k])
    # 倍频纠错：若半 lag（双倍 BPM）的相关性接近，取更快的那个
    if lag % 2 == 0 and lag // 2 >= lo:
        half = float(ac[lag // 2])
        if half >= peak * 0.62:
            lag = lag // 2
            peak = max(peak, half)
    lag2 = 2 * lag
    harm = float(ac[lag2]) if lag2 < ac.size else 0.0
    regular = float(np.clip(peak + max(harm, 0.0) * 0.5, 0, 1))
    bpm = 60.0 * fps / lag
    while bpm > 195:
        bpm /= 2
    return float(bpm), regular


def _modulation(y: np.ndarray, Sm: np.ndarray = None, fp: np.ndarray = None) -> float:
    """幅度调制的「节拍感 vs 语声感」比值。
    实测：器乐/EDM 有稳定节拍调制 -> 比值高(纯音乐 med 5.9)；
          人声流行频谱调制分散 -> 比值低(方格 med 1.9)。
    Sm/fp 传入可复用主 STFT（同参数 2048/512）。
    """
    fps = SR / 512.0
    if Sm is None:
        f, t, S = _stft(y, 2048, 512)
        fp, Sm = f, S
    m = (fp >= 300) & (fp < 3500)
    env = Sm[m].sum(0).astype(np.float64)
    n = min(env.size, int(fps * 45))
    if n < 64:
        return 3.0
    env = env[:n] - env[:n].mean()
    env *= np.hanning(n)
    M = np.abs(np.fft.rfft(env))
    fr = np.fft.rfftfreq(n, 1.0 / fps)
    peak = M.max() + 1e-9
    syl = float(M[(fr >= 1.5) & (fr <= 10)].max() / peak)
    slow = float(M[(fr >= 0.2) & (fr <= 1.0)].mean() / peak)
    fast = float(M[(fr >= 12) & (fr <= 40)].mean() / peak)
    return float(syl / (slow + fast + 1e-9))


# ------------------------------------------------------------------ 主入口
# 阈值来自对 D:\纯音乐 / D:\方格 / D:\废歌 各 10 首的实测分布（详见 README「校准」）
def extract(path: str, seconds: float = 100.0) -> dict:
    y = decode(path, seconds)
    if y.size < SR // 2 or not np.isfinite(y).any():
        raise ValueError("empty/undecodable audio")

    y = np.array(y, dtype=np.float32, copy=True)
    y = np.nan_to_num(y, nan=0.0, posinf=0.0, neginf=0.0)
    y = np.clip(y, -1.0, 1.0)
    peak = float(np.max(np.abs(y))) or 1.0
    y = y / peak                                     # 峰值归一，消除响度战争偏差

    hop = 512
    f, t, S = _stft(y, 2048, hop)                    # S: (freq, frame) 幅度谱
    ok = f > 0
    fp, Sm = f[ok], S[ok]
    P = Sm ** 2
    tot = float(P.sum()) + 1e-10

    def band(a: float, b: float) -> float:
        m = (fp >= a) & (fp < b)
        return float(P[m].sum() / tot) if m.any() else 0.0

    # --- 能量：帧 RMS 中位数（实测 p25=0.09 med=0.19 p75=0.27）
    frames = y[:(y.size // hop) * hop].reshape(-1, hop)
    frame_rms = np.sqrt(np.mean(frames ** 2, axis=1))
    med_rms = float(np.median(frame_rms)) if frame_rms.size else 0.0
    dyn = float(np.percentile(frame_rms, 90) - np.percentile(frame_rms, 10)) if frame_rms.size else 0.0
    energy = _norm(med_rms, 0.03, 0.50)          # hi 提到 0.50：避免 DJ 母带全部饱和成 1.0
    dynamic = _norm(dyn, 0.05, 0.45)

    # --- 亮度：幅度谱质心（实测 900~2400Hz，与 librosa 对齐）
    cent = float(np.median((Sm * fp[:, None]).sum(0) / (Sm.sum(0) + 1e-10)))
    brightness = _norm(cent, 700, 2800)

    # --- 频谱平坦度（实测 纯音乐 0.13 / 流行 0.23 / 噪杂 0.26）
    flat_raw = float(np.median(np.exp(np.mean(np.log(Sm), axis=0)) / (Sm.mean(axis=0) + 1e-10)))
    flatness = _norm(flat_raw, 0.02, 0.42)

    # --- 谐波集中度：top5% 频点占用的能量（实测 纯音乐 .65 / 流行 .60 / 噪声 .48）
    k = max(1, int(Sm.shape[0] * 0.05))
    peak5 = float(np.mean(np.sort(Sm, axis=0)[-k:].sum(0) / (Sm.sum(0) + 1e-10)))
    tonality = _norm(peak5, 0.40, 0.85)

    # --- 粗糙度：过零率 + 极高频占比
    #     实测 b_vh：纯音乐 .002 / 流行 .010 / 失真 0.02+；zcr：.075 / .098 / .13
    #     （早期 *300 缩放会饱和到 1.0，使摇滚门槛失效，改为按实测区间归一）
    zcr = float(np.mean(np.abs(np.diff(np.sign(y))) > 0))
    b_vh = band(7000, 11000)
    roughness = _norm(0.6 * _norm(zcr, 0.045, 0.13) + 0.4 * _norm(b_vh, 0.0005, 0.02), 0.15, 0.85)

    # --- 幅度调制比：器乐/EDM(高) vs 人声流行(低)
    #     实测 med：纯音乐 5.9 / 方格 1.9 / 废歌 3.6
    mod_ratio = _modulation(y, Sm, fp)
    mod = _norm(mod_ratio, 1.0, 7.0)           # 1=节拍调制规整(器乐感), 0=调制分散(人声感)

    # --- 人声存在感：齿音带 + 极高频 + 平坦度 - 谐波集中度，并用调制比校正
    #     （EDM 频谱平坦但调制规整，早期会被误判成人声；乘 (1-mod) 抑制）
    b_hi = band(2500, 7000)
    b_low = band(0, 300)
    v_raw = (0.42 * _norm(b_hi, 0.005, 0.06) + 0.18 * _norm(b_vh, 0.0005, 0.02)
             + 0.25 * flatness + 0.15 * (1 - tonality))
    v_raw *= (0.70 + 0.30 * (1 - mod))
    vocal = _norm(v_raw, 0.10, 0.95)

    # --- 节拍（_onset_env: nperseg=1024, hop=512 -> 43.07 帧/秒）
    env = _onset_env(y)
    bpm, regular = _tempo(env, 512 / SR)

    # --- 原声/器乐倾向：低平坦 + 高谐波集中 + 规整调制
    a_raw = 0.40 * (1 - flatness) + 0.35 * tonality + 0.25 * mod
    acoustic = _norm(a_raw, 0.35, 0.86)
    # --- 明亮/正向：亮度 + 谐波集中 - 低频堆积
    valence = _norm(0.50 * brightness + 0.30 * tonality + 0.20 * (1 - flatness)
                    - 0.10 * _norm(b_low, 0.30, 0.75), 0.15, 0.85)

    # --- 律动/可舞：节拍规整 + 舒适 BPM + 能量
    danceability = _norm(regular * 0.55 + max(0.0, 1 - abs(bpm - 120) / 65.0) * 0.25
                         + energy * 0.30, 0.05, 1.0) if bpm > 0 else _norm(energy * 0.35, 0, 1)

    return {
        "tempo": round(bpm, 1),
        "energy": round(energy, 4),
        "danceability": round(danceability, 4),
        "acoustic": round(acoustic, 4),
        "valence": round(valence, 4),
        "brightness": round(brightness, 4),
        "roughness": round(roughness, 4),
        "flatness": round(flatness, 4),
        "vocal": round(vocal, 4),
        "v_raw": round(float(v_raw), 4),
        "tonality": round(tonality, 4),
        "a_raw": round(float(a_raw), 4),
        "dynamic": round(dynamic, 4),
        "mod": round(mod, 4),
        "mod_ratio": round(mod_ratio, 2),
        "duration": round(y.size / SR, 2),
    }
