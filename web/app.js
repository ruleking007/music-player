/* 氛围音乐播放器 · 前端逻辑（无框架、无依赖） */
'use strict';
const $ = (s, p = document) => p.querySelector(s);
const $$ = (s, p = document) => [...p.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const fmt = t => { t = Math.max(0, t | 0); return `${(t / 60) | 0}:${String(t % 60).padStart(2, '0')}`; };

const api = {
  async get(u) { const r = await fetch(u, { cache: 'no-store' }); if (!r.ok) throw new Error((await r.json().catch(_ => {})).error || r.status); return r.json(); },
  async post(u, body) {
    const r = await fetch(u, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
    const d = await r.json().catch(_ => ({}));
    if (!r.ok) throw new Error(d.error || r.status);
    return d;
  }
};

const S = {
  playlist: 'all', mood: '', q: '', mode: 'song', order: '',
  songs: [], total: 0, index: -1, cur: null, queue: [],
  lyric: null, lyricIdx: -1, playing: false, loop: 'list', // list | one | shuffle
  pollTimer: null, cfg: null,
};

/* 音质 / 去重 视图状态 */
const Q = { filter: 'upgrade', query: '', deepTimer: null, loaded: false,
  selected: new Set(), preview: null };

const audio = $('#audio');
audio.volume = 0.85;
const FALLBACK_COVER = "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><rect width='64' height='64' fill='%2323262e'/><text x='32' y='42' font-size='26' text-anchor='middle' fill='%235b6273'>♪</text></svg>";

/* ---------------------------------------------------------------- 渲染 */
function moodColor(k) {
  return { hype: '#f0736a', groove: '#f0a35e', rock: '#c96af0', pop: '#e8d16a', cheer: '#63c7b8',
           ballad: '#6aa8f0', sad: '#7f8ba3', chill: '#8fd06a', ambient: '#5ec8f0',
           all: '#8b93a7', pending: '#5b6273' }[k] || '#8b93a7';
}

async function loadPlaylists() {
  const rows = await api.get('/api/playlists');
  const nav = $('#playlists');
  nav.innerHTML = '';
  for (const p of rows) {
    const el = document.createElement('div');
    el.className = 'pl' + (p.key === S.playlist ? ' on' : '');
    el.dataset.key = p.key;
    el.style.setProperty('--c', moodColor(p.key));
    el.innerHTML = `<span class="em">${esc(p.emoji || '♪')}</span><span class="nm">${esc(p.name)}</span><span class="ct">${p.n}</span>`;
    el.onclick = () => { S.playlist = p.key; S.mood = p.key === 'all' ? '' : p.key; loadPlaylists(); loadSongs(); };
    nav.appendChild(el);
  }
  const sel = rows.find(r => r.key === S.playlist);
  $('#plname').textContent = sel ? sel.name : '全部歌曲';
  $('#pldesc').textContent = sel?.description || '';
  $('#plcount').textContent = S.total ? `${S.total} 首` : '';
}

async function loadSongs(keep = true) {
  const params = new URLSearchParams({ limit: '400', playlist: S.playlist || '' });
  if (S.q) params.set('q', S.q);
  if (S.order) params.set('order', S.order);
  const d = await api.get('/api/songs?' + params);
  S.songs = d.songs; S.total = d.total;
  $('#plcount').textContent = `${d.total} 首`;
  $('#empty').classList.toggle('hidden', d.total > 0);
  const ul = $('#songs');
  const keepTop = keep ? ul.scrollTop : 0;
  ul.innerHTML = '';
  const frag = document.createDocumentFragment();
  d.songs.forEach((s, i) => {
    const li = document.createElement('li');
    li.className = 'row' + (S.cur && S.cur.id === s.id ? ' on' : '');
    li.dataset.i = i;
    const mood = s.mood_label || (s.analyzed ? '' : '待分析');
    li.innerHTML = `<span class="idx">${i + 1}</span>
      <div class="tt"><b>${esc(s.title)}</b><div class="ar">${esc(s.artist || '未知歌手')} — ${esc(s.dir.split(/[\\\\/]/).pop())}</div></div>
      <span class="tags">
        ${s.failed ? '<span class="chip warn">解析失败</span>' : ''}
        ${s.lyric_id ? '<span class="lc has" title="有歌词">词</span>' : '<span class="lc" title="无歌词">·</span>'}
        ${s.tempo ? `<span class="chip bpm">${s.tempo | 0} BPM</span>` : ''}
        ${mood ? `<span class="chip mood">${esc(mood)}</span>` : ''}
      </span>
      <span class="dur">${fmt(s.duration)}</span>`;
    li.onclick = () => playIndex(i);
    frag.appendChild(li);
  });
  ul.appendChild(frag);
  ul.scrollTop = keepTop;   // keep=true 时维持原滚动位置（后台分析刷新不打断浏览）
}

async function doSearch() {
  const q = $('#q').value.trim();
  if (S.mode === 'quality') { Q.query = q; loadQuality(); return; }
  S.q = q;
  if (!q) { if (S.mode === 'song') loadSongs(); else renderLyricResults([]); return; }
  if (S.mode === 'lyric') {
    const d = await api.get('/api/search?q=' + encodeURIComponent(q));
    renderLyricResults(d.results);
  } else {
    loadSongs();
  }
}

function renderLyricResults(rows) {
  const ul = $('#songs');
  ul.innerHTML = '';
  $('#empty').classList.toggle('hidden', rows.length > 0);
  $('#plcount').textContent = `${rows.length} 条命中`;
  rows.forEach(r => {
    const li = document.createElement('li');
    li.className = 'row';
    li.innerHTML = `<span class="idx">♪</span>
      <div class="tt"><b>${esc(r.title)}</b><div class="ar">${esc(r.hits.join(' / ') || r.artist || '')}</div></div>
      <span class="tags">${r.mood_label ? `<span class="chip mood">${esc(r.mood_label)}</span>` : ''}</span>
      <span class="dur">${fmt(r.duration)}</span>`;
    li.onclick = async () => {
      const s = await api.get('/api/song/' + r.id);
      S.songs = [s]; S.queue = [s]; S.index = 0; playCurrent(r.hit_time);
    };
    ul.appendChild(li);
  });
}

/* ---------------------------------------------------------------- 播放 */
async function playIndex(i) {
  S.queue = S.songs.slice(); S.index = i;
  await playCurrent();
}

async function playCurrent(seekTo) {
  const s = S.queue[S.index];
  if (!s) return;
  S.cur = s;
  $$('#songs .row').forEach(el => el.classList.toggle('on', +el.dataset.i === S.index));
  $('#ptitle').textContent = s.title || s.name;
  $('#partist').textContent = [s.artist, s.album].filter(Boolean).join(' · ') || s.name;
  $('#moodchip').textContent = s.mood_label || '';
  $('#moodchip').style.color = moodColor(s.mood);
  $('#bpm').textContent = s.tempo ? `${s.tempo | 0} BPM` : '—';
  $('#cover').onerror = () => { $('#cover').src = FALLBACK_COVER; $('#cover').onerror = null; };
  $('#cover').src = `/cover/${s.id}?v=${s.size || 0}`;
  audio.src = `/audio/${s.id}`;
  audio.load();
  if (seekTo != null && seekTo >= 0) {
    const ready = () => { audio.currentTime = seekTo; audio.removeEventListener('loadedmetadata', ready); };
    audio.addEventListener('loadedmetadata', ready);
  }
  try { await audio.play(); } catch (e) { /* 需要用户手势 */ }
  showInfo(s);
  openLyricPanel();
  loadLyric(s.id);
}

function step(dir) {
  if (!S.queue.length) return;
  if (S.loop === 'shuffle') S.index = (Math.random() * S.queue.length) | 0;
  else S.index = (S.index + dir + S.queue.length) % S.queue.length;
  playCurrent();
}

audio.addEventListener('play', () => { S.playing = true; $('#b-play').textContent = '⏸'; });
audio.addEventListener('pause', () => { S.playing = false; $('#b-play').textContent = '▶'; });
audio.addEventListener('ended', () => { if (S.loop === 'one') { audio.currentTime = 0; audio.play(); } else step(1); });
audio.addEventListener('error', () => { if (S.cur) toast(`无法播放：${S.cur.title || S.cur.name}`); });
audio.addEventListener('timeupdate', () => {
  const d = audio.duration || S.cur?.duration || 0;
  $('#t-cur').textContent = fmt(audio.currentTime);
  $('#t-dur').textContent = fmt(d);
  const p = d ? (audio.currentTime / d) * 100 : 0;
  $('#seek').value = p * 10;
  $('#seek').style.setProperty('--p', p + '%');
  syncLyric(audio.currentTime);
});
audio.addEventListener('progress', () => {
  try { if (audio.buffered.length) audio.playbackRate = 1; } catch (e) {}
});

$('#b-play').onclick = () => {
  if (!S.cur) { if (S.songs.length) playIndex(0); else toast('先扫描或选一个歌单'); return; }
  audio.paused ? audio.play() : audio.pause();
};
$('#b-prev').onclick = () => step(-1);
$('#b-next').onclick = () => step(1);
$('#seek').oninput = e => { if (audio.duration) audio.currentTime = e.target.value / 1000 * audio.duration; };
$('#vol').oninput = e => audio.volume = e.target.value / 100;
$('#b-mode').onclick = () => {
  const modes = ['list', 'one', 'shuffle'], icons = { list: '🔁', one: '🔂', shuffle: '🔀' };
  S.loop = modes[(modes.indexOf(S.loop) + 1) % 3];
  $('#b-mode').textContent = icons[S.loop];
  toast(S.loop === 'list' ? '列表循环' : S.loop === 'one' ? '单曲循环' : '随机播放');
};

/* ---------------------------------------------------------------- 歌词 */
async function loadLyric(id) {
  S.lyric = null; S.lyricIdx = -1;
  try { S.lyric = await api.get('/api/lyric/' + id); } catch (e) { S.lyric = null; }
  const box = $('#lrc');
  const lines = (S.lyric?.lines || []).filter(l => l.text);
  if (!lines.length) {
    box.innerHTML = `<p class="muted">这首歌还没有歌词。可在「分析」页粘贴 LRC，或去 ⚙ 导入歌词库。</p>`;
    return;
  }
  box.innerHTML = lines.map((l, i) => `<p data-i="${i}" data-t="${l.t}">${esc(l.text)}</p>`).join('');
  $$('#lrc p').forEach(p => p.onclick = () => {
    const t = +p.dataset.t; if (t >= 0 && audio.duration) audio.currentTime = t;
  });
}

function syncLyric(t) {
  const ps = $$('#lrc p');
  if (!ps.length) return;
  let idx = -1;
  for (let i = 0; i < ps.length; i++) { if (+ps[i].dataset.t <= t + 0.35) idx = i; else break; }
  if (idx === S.lyricIdx) return;
  S.lyricIdx = idx;
  ps.forEach((p, i) => {
    p.classList.toggle('cur', i === idx);
    p.classList.toggle('past', i < idx);
  });
  if (idx >= 0) ps[idx].scrollIntoView({ block: 'center', behavior: 'smooth' });
}

$('#b-lyric').onclick = () => $('#lyricpanel').classList.toggle('hidden');
function openLyricPanel() { $('#lyricpanel').classList.remove('hidden'); $('#b-lyric').classList.add('on'); }
$('#lp-close').onclick = () => { $('#lyricpanel').classList.add('hidden'); $('#b-lyric').classList.remove('on'); };
$$('.lp-tabs button').forEach(b => b.onclick = () => {
  $$('.lp-tabs button').forEach(x => x.classList.toggle('on', x === b));
  $('#lp-lyric').classList.toggle('hidden', b.dataset.t !== 'lyric');
  $('#lp-info').classList.toggle('hidden', b.dataset.t !== 'info');
});
$('#lrcsave').onclick = async () => {
  if (!S.cur) return toast('先选一首歌');
  const text = $('#lrcedit').value.trim();
  if (!text) return toast('歌词内容为空');
  try {
    const r = await api.post(`/api/lyric/${S.cur.id}`, { lrc: text });
    toast(`已保存 ${r.lines} 行`);
    loadLyric(S.cur.id); showInfo(S.cur);
  } catch (e) { toast('保存失败：' + e.message); }
};

function showInfo(s) {
  $('#lp-title').textContent = s.title || s.name;
  $('#lp-artist').textContent = [s.artist, s.album].filter(Boolean).join(' · ');
  const F = [['能量', 'energy'], ['律动', 'danceability'], ['原声', 'acoustic'], ['明亮', 'valence'],
             ['人声', 'vocal'], ['粗糙', 'roughness'], ['平坦', 'flatness'], ['BPM/180', 'tempo']];
  $('#feats').innerHTML = F.map(([n, k]) => {
    const v = k === 'tempo' ? (s.tempo || 0) / 180 : (s[k] ?? 0);
    return `<div class="feat"><span>${n}<b>${((s[k] ?? 0) * (k === 'tempo' ? 1 : 100)).toFixed(k === 'tempo' ? 0 : 2)}</b></span>
            <div class="fbar"><i style="width:${Math.min(100, v * 100)}%"></i></div></div>`;
  }).join('');
  $('#moodreason').textContent = s.mood_reason || '';
}
// 点击行时同步信息面板
document.addEventListener('click', e => {
  const row = e.target.closest('.row');
  if (!row) return;
  const s = S.songs[+row.dataset.i];
  if (s) showInfo(s);
});

/* ---------------------------------------------------------------- 状态轮询 */
async function pollState() {
  try {
    const d = await api.get('/api/state');
    const st = d.state, c = d.counts || {};
    $('#bootinfo').textContent = `曲库 ${c.total || 0} 首 · 已分析 ${c.analyzed || 0}`;
    const busy = st.scanning || st.analyzing;
    $('#progress').classList.toggle('hidden', !busy);
    if (busy) {
      const done = st.scanning ? st.scan_done : st.feat_done;
      const total = Math.max(1, st.scanning ? (st.scan_total || 1) : (st.feat_total || 1));
      $('#barfill').style.width = Math.min(100, done / total * 100) + '%';
      $('#bartext').textContent = st.scanning ? `扫描 ${done}/${total} ${st.scan_phase}` :
        `分析 ${done}/${total}（失败 ${st.feat_failed}）`;
      if (c.analyzed !== S._lastAnalyzed) { S._lastAnalyzed = c.analyzed; loadSongs(true); loadPlaylists(); }
    }
    if (st.note) $('#bartext').title = st.note;
  } catch (e) { /* 服务重启中 */ }
}

/* ---------------------------------------------------------------- 设置 */
async function openSettings() {
  S.cfg = await api.get('/api/config');
  $('#cfg-roots').value = (S.cfg.scan_roots || []).join('\n');
  $('#cfg-excl').value = (S.cfg.exclude_dirs || []).join(', ');
  $('#cfg-secs').value = S.cfg.analyze_seconds;
  $('#cfg-workers').value = S.cfg.analyze_workers;
  $('#cfg-lyr').value = (S.cfg.lyrics_import_dirs || []).join('\n');
  $('#drawer').classList.remove('hidden'); $('#mask').classList.remove('hidden');
}
$('#btn-settings').onclick = openSettings;
$('#cfg-close').onclick = () => { $('#drawer').classList.add('hidden'); $('#mask').classList.add('hidden'); };
$('#mask').onclick = $('#cfg-close').onclick;
$('#cfg-save').onclick = async () => {
  const body = {
    scan_roots: $('#cfg-roots').value.split('\n').map(s => s.trim()).filter(Boolean),
    exclude_dirs: $('#cfg-excl').value.split(',').map(s => s.trim()).filter(Boolean),
    analyze_seconds: +$('#cfg-secs').value || 100,
    analyze_workers: +$('#cfg-workers').value || 2,
    lyrics_import_dirs: $('#cfg-lyr').value.split('\n').map(s => s.trim()).filter(Boolean),
  };
  await api.post('/api/config', body);
  toast('已保存配置');
  $('#cfg-close').onclick();
};
$('#import-lyr').onclick = async () => {
  const dirs = $('#cfg-lyr').value.split('\n').map(s => s.trim()).filter(Boolean);
  if (!dirs.length) return toast('先填写歌词库目录');
  for (const d of dirs) {
    try {
      const r = await api.post('/api/import', { dir: d, remember: true });
      toast(`${d.split(/[\\\\/]/).pop()}：匹配 ${r.matched || 0} 首`);
    } catch (e) { toast('导入失败：' + e.message); }
  }
  loadSongs(false);
};

$('#btn-scan').onclick = async () => {
  await api.post('/api/scan');
  toast('开始扫描并分析新歌…');
  setTimeout(pollState, 300);
};

/* ---------------------------------------------------------------- 交互 */
$$('.tab').forEach(b => b.onclick = () => {
  $$('.tab').forEach(x => x.classList.toggle('on', x === b));
  S.mode = b.dataset.mode;
  const isQ = S.mode === 'quality';
  $('#quality').classList.toggle('hidden', !isQ);
  $('#songs').classList.toggle('hidden', isQ);
  $('#crumb').classList.toggle('hidden', isQ);
  $('#order').classList.toggle('hidden', isQ);
  $('#empty').classList.toggle('hidden', isQ || S.total > 0);
  if (isQ) {
    $('#q').placeholder = '筛选音质清单：歌名 / 歌手';
    $('#q').value = Q.query;
    loadQuality();
  } else {
    $('#q').placeholder = S.mode === 'lyric' ? '搜索歌词内容，如「一百万个可能」' : '搜索歌名 / 歌手 / 专辑 / 文件夹';
    $('#q').value = S.q;
    S.q ? doSearch() : (S.mode === 'song' ? loadSongs() : renderLyricResults([]));
  }
  syncClearBtn();
});
let t1;
function syncClearBtn() { $('#q-clear').classList.toggle('hidden', !$('#q').value); }
function clearSearch() {
  $('#q').value = ''; S.q = ''; Q.query = '';
  clearTimeout(t1); syncClearBtn();
  if (S.mode === 'quality') loadQuality();
  else if (S.mode === 'lyric') renderLyricResults([]);
  else loadSongs();
}
$('#q-clear').onclick = clearSearch;
$('#q').oninput = () => { syncClearBtn(); clearTimeout(t1); t1 = setTimeout(doSearch, 260); };
$('#q').onkeydown = e => {
  if (e.key === 'Enter') { clearTimeout(t1); doSearch(); }
  else if (e.key === 'Escape' && $('#q').value) { e.stopPropagation(); clearSearch(); }
};
$('#order').onchange = e => { S.order = e.target.value; loadSongs(false); };

document.addEventListener('keydown', e => {
  if (e.target.matches('input,textarea,select')) {
    if (e.key === 'Escape') e.target.blur();
    return;
  }
  if (e.code === 'Space') { e.preventDefault(); $('#b-play').click(); }
  else if (e.key === 'ArrowRight') { audio.currentTime += 5; }
  else if (e.key === 'ArrowLeft') { audio.currentTime -= 5; }
  else if (e.key === 'ArrowDown') { e.preventDefault(); step(1); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); step(-1); }
  else if (e.key === 'l' || e.key === 'L') { $('#b-lyric').click(); }
  else if (e.key === '/') { e.preventDefault(); $('#q').focus(); }
});

let tt;
function toast(msg) {
  const el = $('#toast');
  el.textContent = msg; el.classList.add('show');
  clearTimeout(tt); tt = setTimeout(() => el.classList.remove('show'), 2200);
}

/* ---------------------------------------------------------------- 启动 */
(async function boot() {
  await loadPlaylists();
  await loadSongs();
  pollState();
  S.pollTimer = setInterval(pollState, 2000);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) clearInterval(S.pollTimer);
    else S.pollTimer = setInterval(pollState, 2000);
  });
})();

/* ---------------------------------------------------------------- 音质 / 去重 */
const fmtSize = b => !b ? '0 MB' : b >= 1073741824 ? (b / 1073741824).toFixed(2) + ' GB'
  : Math.round(b / 1048576) + ' MB';
const qColor = s => s >= 80 ? '#63c7b8' : s >= 60 ? '#6aa8f0' : s >= 40 ? '#f0a35e' : '#f28b8b';

function qTech(r) {
  const a = [(r.ext || '').replace('.', '').toUpperCase(), (r.q_kbps || 0) + ' kbps'];
  if (r.q_sr) a.push((r.q_sr / 1000).toFixed(1) + ' kHz');
  if (r.q_cutoff > 0) a.push('截止 ' + (r.q_cutoff / 1000).toFixed(1) + ' kHz');
  return a.filter(Boolean).join(' · ');
}

function qRowHTML(r, selectable) {
  const s = +r.q_score || 0, c = qColor(s);
  const canPick = !!selectable && !r.dup_best && /可清理/.test(r.q_advice || '');
  const picked = Q.selected.has(r.id);
  const chk = selectable ? `<label class="q-chk" title="${canPick ? '选中以清理这个低质副本' : '最佳版本 / 非重复副本，不可清理'}">
      <input type="checkbox" class="q-chkbox" data-id="${r.id}"${canPick ? '' : ' disabled'}${picked ? ' checked' : ''}></label>` : '';
  return `<div class="q-row${selectable ? ' sel' : ''}${selectable && picked ? ' picked' : ''}" data-id="${r.id}">
    ${chk}
    <div class="qt"><b>${esc(r.title || r.name || '—')}</b>
      <div class="ar" title="${esc(r.q_reason || '')}">${esc(r.artist || '未知歌手')} · ${esc(qTech(r))}</div></div>
    <div class="qscore"><b style="color:${c}">${s.toFixed(0)}</b>
      <div class="qbar"><i style="width:${s}%;background:${c}"></i></div></div>
    <span class="chip ${r.q_fake ? 'warn' : ''}">${esc(r.q_label || '未知')}</span>
    <span class="qadv ${r.dup_best ? 'keep' : ''}">${esc(r.suggest || r.q_advice || '')}</span>
    <div class="qops">
      <button data-act="play" title="播放这首">▶</button>
      <button data-act="copy" data-path="${esc(r.path || '')}" title="复制完整文件路径">⧉</button>
    </div>
  </div>`;
}

function renderQStats(o) {
  const cards = [
    ['曲库总数', o.total || 0, 'var(--fg)'],
    ['真无损', o.lossless || 0, '#63c7b8'],
    ['待换无损', o.upgrade_songs || 0, '#f0a35e'],
    ['重复组', o.dup_groups || 0, '#6aa8f0'],
    ['疑似假无损', o.fake || 0, '#f28b8b'],
    ['可释放空间', fmtSize(o.reclaim_bytes), 'var(--fg)'],
  ];
  $('#q-stats').innerHTML = cards.map(([n, v, c]) =>
    `<div class="q-stat"><b style="color:${c}">${v}</b><span>${n}</span></div>`).join('');
}

function renderQProgress(o) {
  $('#q-progress').classList.toggle('hidden', !o.deep_running && !o.deep_note);
  const pct = o.deep_total ? Math.min(100, (o.deep_progress || 0) / o.deep_total * 100) : 0;
  $('#q-bar').style.width = pct + '%';
  $('#q-bartext').textContent = o.deep_running
    ? `频谱检测 ${o.deep_progress || 0}/${o.deep_total || 0} · 已发现疑似假无损 ${o.fake || 0} 首`
    : (o.deep_note || `已检测 ${o.deep_done || 0}/${o.deep_candidates || 0} 首无损候选`);
}

async function loadQuality() {
  try {
    const o = await api.get('/api/quality/overview');
    renderQStats(o); renderQProgress(o);
    const box = $('#q-list');
    if (Q.filter === 'dup') {
      const d = await api.get('/api/quality/duplicates?limit=200');
      box.innerHTML = d.groups.length ? d.groups.map(g => `
        <div class="qgroup">
          <div class="qg-head"><b>${esc(g.title || '—')}</b>
            <span>${esc(g.artist || '')} · ${g.members.length} 个版本 · 可释放 ${fmtSize(g.reclaim)}</span></div>
          ${g.members.map(m => qRowHTML(m, true)).join('')}
        </div>`).join('') + (d.total > d.groups.length
          ? `<p class="muted q-more">仅显示前 ${d.groups.length}/${d.total} 组</p>` : '')
        : '<p class="muted">没有发现重复组。</p>';
    } else {
      const u = new URLSearchParams({ filter: Q.filter, limit: '300' });
      if (Q.query) u.set('q', Q.query);
      const d = await api.get('/api/quality/list?' + u);
      box.innerHTML = d.rows.length ? d.rows.map(r => qRowHTML(r, false)).join('')
        + (d.total > d.rows.length
          ? `<p class="muted q-more">仅显示前 ${d.rows.length}/${d.total} 条，点「导出清单」获取完整列表</p>` : '')
        : '<p class="muted">这个分类下没有歌曲。</p>';
    }
    Q.loaded = true;
    updateSelBar();
  } catch (e) { toast('音质数据加载失败：' + e.message); }
}

function updateSelBar() {
  const isDup = Q.filter === 'dup';
  const n = Q.selected.size;
  $('#q-select').disabled = !isDup;
  $('#q-trash').disabled = !isDup || n === 0;
  $('#q-selbar').classList.toggle('hidden', !isDup || n === 0);
  $('#q-selinfo').innerHTML = `已选 <b>${n}</b> 首低质副本 · 单批上限 10 首`;
}

$$('#q-filters button').forEach(b => b.onclick = () => {
  $$('#q-filters button').forEach(x => x.classList.toggle('on', x === b));
  Q.filter = b.dataset.f; Q.query = ''; Q.selected.clear(); Q.preview = null;
  if (S.mode === 'quality') { $('#q').value = ''; $('#q-clear').classList.add('hidden'); }
  loadQuality();
});

$('#q-list').addEventListener('click', async e => {
  const btn = e.target.closest('button[data-act]');
  if (!btn) return;
  if (btn.dataset.act === 'copy') {
    try { await navigator.clipboard.writeText(btn.dataset.path); toast('完整路径已复制'); }
    catch (_) { toast('复制失败，请用「导出清单」查看路径'); }
    return;
  }
  if (btn.dataset.act === 'play') {
    const id = +btn.closest('.q-row').dataset.id;
    try {
      const s = await api.get('/api/song/' + id);
      S.songs = [s]; S.queue = [s]; S.index = 0;
      playCurrent();
    } catch (err) { toast('播放失败：' + err.message); }
  }
});

$('#q-refresh').onclick = async () => {
  try {
    const r = await api.post('/api/quality/refresh', {});
    toast(`已重算 ${r.scored} 首 · 重复组 ${r.dup_groups} · 完全相同 ${r.exact_groups}`);
    loadQuality();
  } catch (e) { toast('重算失败：' + e.message); }
};

$('#q-deep').onclick = async () => {
  try {
    const r = await api.post('/api/quality/deep', { workers: 2 });
    if (!r.ok) return toast(r.reason || '检测未启动');
    toast(`开始深度检测 ${r.queued} 首无损候选…`);
    if (Q.deepTimer) clearInterval(Q.deepTimer);
    Q.deepTimer = setInterval(pollDeep, 1200);
    pollDeep();
  } catch (e) { toast('检测启动失败：' + e.message); }
};

async function pollDeep() {
  try {
    const s = await api.get('/api/quality/state');
    $('#q-progress').classList.remove('hidden');
    $('#q-bar').style.width = (s.total ? Math.min(100, s.done / s.total * 100) : 0) + '%';
    $('#q-bartext').textContent = s.running
      ? `频谱检测 ${s.done}/${s.total} · 已发现疑似假无损 ${s.fake} 首`
      : (s.note || '检测结束');
    if (!s.running) { clearInterval(Q.deepTimer); Q.deepTimer = null; loadQuality(); }
  } catch (_) { /* 服务重启中 */ }
}

$('#q-export').onclick = () => {
  const kind = Q.filter === 'dup' ? 'dup' : Q.filter === 'all' ? 'all' : 'upgrade';
  const a = document.createElement('a');
  a.href = '/api/quality/export?kind=' + kind;
  a.download = '';
  document.body.appendChild(a); a.click(); a.remove();
  toast('清单已导出为 CSV（含完整路径）');
};

/* ---------------------------------------------------------------- 安全清除 */
function openModal(title, html, buttons) {
  $('#q-modal-title').textContent = title;
  $('#q-modal-body').innerHTML = html;
  const foot = $('#q-modal-foot');
  foot.innerHTML = '';
  buttons.forEach(b => {
    const el = document.createElement('button');
    el.textContent = b.label;
    if (b.cls) el.className = b.cls;
    el.onclick = b.fn;
    foot.appendChild(el);
  });
  $('#q-modal').classList.remove('hidden');
}
function closeModal() {
  $('#q-modal').classList.add('hidden');
  Q.preview = null;
}
$('#q-modal-close').onclick = closeModal;
$('#q-modal').addEventListener('click', e => { if (e.target.id === 'q-modal') closeModal(); });
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && !$('#q-modal').classList.contains('hidden')) closeModal();
});

$('#q-list').addEventListener('change', e => {
  const cb = e.target.closest('.q-chkbox');
  if (!cb) return;
  const id = +cb.dataset.id;
  if (cb.checked) {
    if (Q.selected.size >= 10) {
      cb.checked = false;
      toast('单批最多 10 首，请先清除这一批');
      return;
    }
    Q.selected.add(id);
  } else Q.selected.delete(id);
  cb.closest('.q-row').classList.toggle('picked', cb.checked);
  updateSelBar();
});

$('#q-select').onclick = () => {
  if (Q.filter !== 'dup') return toast('请先切到「重复组」');
  let n = Q.selected.size;
  for (const b of $$('.q-chkbox:not(:disabled)')) {
    if (n >= 10) break;
    if (b.checked) continue;
    b.checked = true;
    b.closest('.q-row').classList.add('picked');
    Q.selected.add(+b.dataset.id);
    n++;
  }
  updateSelBar();
  toast(n >= 10 ? '已选满 10 首（单批上限），确认后可继续下一批' : `已选中 ${n} 首可清理副本`);
};

$('#q-selclear').onclick = () => {
  Q.selected.clear();
  $$('.q-chkbox').forEach(b => { b.checked = false; b.closest('.q-row').classList.remove('picked'); });
  updateSelBar();
};

$('#q-trash').onclick = async () => {
  if (!Q.selected.size) return;
  if (S.cur && Q.selected.has(S.cur.id)) return toast('正在播放的歌在选中列表里，请先取消勾选');
  try {
    const p = await api.post('/api/quality/trash/preview', { ids: [...Q.selected] });
    if (!p.ok) {
      toast(p.error || '预览失败');
      if (p.detail && p.detail.length) console.warn('已跳过：', p.detail);
      return;
    }
    Q.preview = p;
    const items = p.items.map(it => `
      <div class="tr-item">
        <b>${esc(it.title)}</b> <span class="muted">${esc(it.artist || '')} · ${fmtSize(it.size)} · ${esc(it.q_label || '')} · ${esc(it.advice || '')}</span>
        <div class="tr-path">${esc(it.path)}</div>
        <div class="tr-keep">↳ 同曲组保留：${esc(it.keep_title || '')} — ${esc(it.keep_path || '')}</div>
      </div>`).join('');
    const errs = p.errors && p.errors.length
      ? `<p class="muted">另有 ${p.errors.length} 项被安全校验跳过：${esc(p.errors.join('；'))}</p>` : '';
    openModal('确认清除 · 移入回收站', `
      <p class="tr-warn">⚠️ 此操作会移动你的音频文件，请先逐项核对下面的完整路径！</p>
      <p>共 <b>${p.count}</b> 首 · <b>${fmtSize(p.total_bytes)}</b>。文件将移入 <b>Windows 回收站</b>，
         可随时从回收站还原；每组的最佳版本会保留，绝不会直接删除或清空整组。</p>
      <p class="muted">操作记录写入 <span class="tr-path">data/trash_log.jsonl</span>，含原路径与保留版本，可点「🧾 清理记录」查看。</p>
      ${items}${errs}`,
      [{ label: '取消', fn: closeModal },
       { label: `确认移入回收站（${p.count} 首）`, cls: 'danger', fn: doTrash }]);
  } catch (e) { toast('预览失败：' + e.message); }
};

async function doTrash() {
  const p = Q.preview;
  if (!p) return closeModal();
  const btn = $('#q-modal-foot .danger');
  if (btn) { btn.disabled = true; btn.textContent = '正在移入回收站…'; }
  try {
    const r = await api.post('/api/quality/trash', { ids: p.ids, token: p.token });
    if (!r.ok) { toast(r.error || '清除失败'); closeModal(); return; }
    closeModal();
    Q.selected.clear();
    toast(r.note + (r.failed.length ? ` · ${r.failed.length} 首未移动（见控制台）` : ''));
    if (r.failed.length) console.warn('未能移动的文件：', r.failed);
    // 无论歌曲列表/歌单刷新是否出错，音质页都必须重新拉取，否则状态会停在旧数据
    Q.selected.clear();
    try { await Promise.allSettled([loadPlaylists(), loadSongs()]); }
    finally { await loadQuality(); }
  } catch (e) { toast('清除失败：' + e.message); closeModal(); }
}

$('#q-sync').onclick = async () => {
  try {
    const r = await api.post('/api/quality/sync', {});
    toast(r.removed
      ? `已同步：移除 ${r.removed} 条「文件已不在」的残留记录`
      : '曲库状态已一致，无需同步');
    await Promise.allSettled([loadPlaylists(), loadSongs(), loadQuality()]);
  } catch (e) { toast('同步失败：' + e.message); }
};

$('#q-trashlog').onclick = async () => {
  try {
    const d = await api.get('/api/quality/trash/log?limit=50');
    if (!d.items.length) {
      openModal('清理记录', '<p class="muted">还没有清理记录。以后每次清除都会记在这里，含原路径，方便从回收站找回。</p>',
        [{ label: '关闭', fn: closeModal }]);
      return;
    }
    const rows = d.items.map(it => `
      <div class="tr-item">
        <b>${esc(it.title)}</b> <span class="muted">${esc(it.time)} · ${fmtSize(it.size)} · ${esc(it.method)}</span>
        <div class="tr-path">${esc(it.path)}</div>
        <div class="tr-keep">↳ 保留：${esc(it.keep_path || '')}</div>
        ${it.note ? `<div class="tr-path">${esc(it.note)}</div>` : ''}
      </div>`).join('');
    openModal(`清理记录 · 共 ${d.total} 条`,
      `<p class="muted">记录文件：${esc(d.log)}。全部可从回收站还原；若方式是「隔离目录」，按 note 里的路径移回原处即可。</p>${rows}`,
      [{ label: '关闭', fn: closeModal }]);
  } catch (e) { toast('读取记录失败：' + e.message); }
};
