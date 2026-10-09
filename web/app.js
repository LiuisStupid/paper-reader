/* ══════════════════════════════════════════════════════════════════════════
   Paper Reader — 前端逻辑
   左侧论文（服务端渲染页面 + 像素级对齐的可选文本层），右侧 AI 对话。
   ══════════════════════════════════════════════════════════════════════════ */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  paper: null,
  count: 0,
  pageSizes: [],
  zoom: 1,
  fitScale: 1,
  lang: 'en',
  rendered: new Set(),
  translating: new Set(),
  layout: new Map(),      // page -> layout response
  selection: '',
  focus: null,            // null | 'left' | 'right'
  fitMode: 'width',       // width | page
  streaming: false,
  models: [],
};

/* ══════════════════════════ 基础工具 ══════════════════════════ */

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

let toastTimer = null;
function toast(msg, bad = false) {
  const el = $('#toast');
  el.textContent = msg;
  el.classList.toggle('bad', bad);
  el.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add('hidden'), bad ? 6000 : 2600);
}

function overlay(text) {
  if (text === null) return $('#overlayLoading').classList.add('hidden');
  $('#overlayText').textContent = text;
  $('#overlayLoading').classList.remove('hidden');
}

async function api(path, options = {}) {
  const res = await fetch(path, options);
  const isJson = (res.headers.get('content-type') || '').includes('json');
  const body = isJson ? await res.json().catch(() => ({})) : {};
  if (!res.ok) throw new Error(body.error || body.detail || `请求失败 (${res.status})`);
  return body;
}

/** 解析 text/event-stream，逐事件回调。 */
async function streamSSE(res, onEvent) {
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf('\n\n')) >= 0) {
      const chunk = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      for (const line of chunk.split('\n')) {
        if (!line.startsWith('data:')) continue;
        const raw = line.slice(5).trim();
        if (!raw || raw === '[DONE]') continue;
        try { onEvent(JSON.parse(raw)); } catch (_) { /* 忽略半包 */ }
      }
    }
  }
}

/* ══════════════════════════ Markdown 渲染 ══════════════════════════ */

function inlineMd(text) {
  let out = escapeHtml(text);
  out = out.replace(/`([^`\n]+)`/g, (_, c) => `<code>${c}</code>`);
  out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  out = out.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
  out = out.replace(/~~([^~]+)~~/g, '<del>$1</del>');
  out = out.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener">$1</a>');
  return out;
}

function mdToHtml(src) {
  const lines = String(src || '').replace(/\r\n/g, '\n').split('\n');
  const html = [];
  let i = 0;

  const isTableRow = (s) => /^\s*\|.*\|\s*$/.test(s);

  while (i < lines.length) {
    const line = lines[i];

    // 代码块
    const fence = line.match(/^\s*```(\w*)\s*$/);
    if (fence) {
      const buf = [];
      i++;
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) buf.push(lines[i++]);
      i++;
      html.push(`<pre><code>${escapeHtml(buf.join('\n'))}</code></pre>`);
      continue;
    }

    // 标题
    const head = line.match(/^(#{1,6})\s+(.*)$/);
    if (head) {
      const lvl = Math.min(head[1].length + 1, 6);
      html.push(`<h${lvl}>${inlineMd(head[2])}</h${lvl}>`);
      i++;
      continue;
    }

    // 分割线
    if (/^\s*([-*_])\s*\1\s*\1[\s\-*_]*$/.test(line)) {
      html.push('<hr>');
      i++;
      continue;
    }

    // 表格
    if (isTableRow(line) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      const cells = (s) => s.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
      const head = cells(line);
      i += 2;
      const rows = [];
      while (i < lines.length && isTableRow(lines[i])) rows.push(cells(lines[i++]));
      html.push('<table><thead><tr>' + head.map((c) => `<th>${inlineMd(c)}</th>`).join('') + '</tr></thead><tbody>'
        + rows.map((r) => '<tr>' + r.map((c) => `<td>${inlineMd(c)}</td>`).join('') + '</tr>').join('')
        + '</tbody></table>');
      continue;
    }

    // 引用
    if (/^\s*>\s?/.test(line)) {
      const buf = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ''));
      html.push(`<blockquote>${inlineMd(buf.join(' '))}</blockquote>`);
      continue;
    }

    // 列表
    const isUl = (s) => /^\s*[-*+]\s+/.test(s);
    const isOl = (s) => /^\s*\d+[.)]\s+/.test(s);
    if (isUl(line) || isOl(line)) {
      const ordered = isOl(line);
      const items = [];
      while (i < lines.length && (ordered ? isOl(lines[i]) : isUl(lines[i]))) {
        items.push(lines[i++].replace(/^\s*(?:[-*+]|\d+[.)])\s+/, ''));
      }
      const tag = ordered ? 'ol' : 'ul';
      html.push(`<${tag}>` + items.map((t) => `<li>${inlineMd(t)}</li>`).join('') + `</${tag}>`);
      continue;
    }

    // 空行
    if (!line.trim()) { i++; continue; }

    // 段落：合并到下一个空行 / 块级元素之前
    const buf = [line];
    i++;
    while (i < lines.length && lines[i].trim()
      && !/^\s*(```|#{1,6}\s|>|\s*[-*+]\s|\s*\d+[.)]\s)/.test(lines[i])
      && !isTableRow(lines[i])) {
      buf.push(lines[i++]);
    }
    html.push(`<p>${inlineMd(buf.join(' '))}</p>`);
  }
  return html.join('');
}

/* ══════════════════════════ 书库 / 搜索 ══════════════════════════ */

function cardHtml(item, kind) {
  const pid = item.id;
  const cover = item.has_pdf
    ? `<img src="/api/papers/${encodeURIComponent(pid)}/cover.png" alt="" loading="lazy"
            onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'placeholder',textContent:'📄'}))">`
    : '<div class="placeholder">📄</div>';
  const meta = [item.authors, item.venue, item.year].filter(Boolean).join(' · ');
  const progress = item.n_pages ? `已读 ${item.last_page + 1}/${item.n_pages}` : '';
  if (kind === 'history') {
    return `<div class="card" data-pid="${escapeHtml(pid)}">
      <div class="card-cover">${cover}</div>
      <div class="card-body">
        <div class="card-title">${escapeHtml(item.title || '未命名')}</div>
        <div class="card-meta">${escapeHtml(meta)}</div>
        <div class="card-abstract">${escapeHtml(item.abstract || '（无摘要）')}</div>
        <div class="card-foot">
          <span class="tag">${escapeHtml(progress)}</span>
          <button class="btn small primary" data-act="open">继续阅读</button>
          <button class="btn small" data-act="delete" title="从书库移除">✕</button>
        </div>
      </div></div>`;
  }
  // 搜索结果没有封面，用紧凑卡片，省掉一大块空白占位区
  const pdfTag = item.pdf_url ? '<span class="tag">可下载 PDF</span>' : '<span class="tag">无 PDF</span>';
  return `<div class="card compact">
    <div class="card-body">
      <div class="card-meta">${escapeHtml([item.source === 'arxiv' ? 'arXiv' : 'Semantic Scholar', item.year]
        .filter(Boolean).join(' · '))}</div>
      <div class="card-title">${escapeHtml(item.title || '未命名')}</div>
      <div class="card-meta">${escapeHtml(meta)}</div>
      <div class="card-abstract">${escapeHtml(item.abstract || '（无摘要）')}</div>
      <div class="card-foot">
        ${pdfTag}
        <button class="btn small primary" data-act="open" ${item.pdf_url ? '' : 'disabled'}>打开阅读</button>
      </div>
    </div></div>`;
}

async function loadHistory(q = '') {
  const list = $('#historyList');
  try {
    const { papers } = await api('/api/papers' + (q ? `?q=${encodeURIComponent(q)}` : ''));
    $('#historyMeta').textContent = papers.length ? `${papers.length} 篇` : '';
    list.innerHTML = papers.length
      ? papers.map((p) => cardHtml(p, 'history')).join('')
      : `<div class="empty-state">${q ? '没有匹配的记录' : '还没有阅读记录 —— 打开或搜索一篇论文开始吧'}</div>`;
    $$('.card', list).forEach((card) => {
      const pid = card.dataset.pid;
      card.addEventListener('click', async (e) => {
        const act = e.target.dataset.act;
        if (act === 'delete') {
          e.stopPropagation();
          if (!confirm('从书库中移除这篇论文？（会删除已下载的 PDF 与翻译缓存）')) return;
          await api(`/api/papers/${encodeURIComponent(pid)}`, { method: 'DELETE' });
          toast('已移除');
          loadHistory($('#historyFilter').value.trim());
          return;
        }
        openPaper(pid);
      });
    });
  } catch (err) {
    list.innerHTML = `<div class="empty-state">加载失败：${escapeHtml(err.message)}</div>`;
  }
}

function showProgress(text) {
  const box = $('#searchProgress');
  box.classList.remove('hidden');
  box.classList.add('indeterminate');
  box.querySelector('span').textContent = text;
  box.style.setProperty('--p', '0%');
}

function setProgress(pct, text) {
  const box = $('#searchProgress');
  box.classList.remove('indeterminate');
  box.style.setProperty('--p', `${pct}%`);
  if (text) box.querySelector('span').textContent = text;
}

function hideProgress() {
  $('#searchProgress').classList.add('hidden');
}

async function doSearch(query, source) {
  if (!query) return;
  $('#searchSection').classList.remove('hidden');
  $('#searchResults').innerHTML = '';
  $('#searchMeta').textContent = '';
  showProgress('正在检索…');
  try {
    const data = await api(`/api/search?q=${encodeURIComponent(query)}&source=${source}`);
    const results = data.results || [];
    $('#searchMeta').textContent = results.length ? `${results.length} 条结果` : '';
    $('#searchResults').innerHTML = results.length
      ? results.map((r) => cardHtml(r, 'search')).join('')
      : '<div class="empty-state">没有找到结果，换个关键词试试</div>';
    (data.errors || []).forEach((e) => toast(e, true));

    $$('#searchResults .card').forEach((card, idx) => {
      card.querySelector('[data-act="open"]').addEventListener('click', (e) => {
        e.stopPropagation();
        openSearchResult(results[idx]);
      });
    });
    $('#searchSection').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (err) {
    toast('检索失败：' + err.message, true);
    $('#searchResults').innerHTML = `<div class="empty-state">检索失败：${escapeHtml(err.message)}</div>`;
  } finally {
    hideProgress();
  }
}

async function openSearchResult(hit) {
  overlay('正在下载 PDF…');
  showProgress('开始下载…');
  try {
    const res = await fetch('/api/papers/open-result/stream', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(hit),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.error || `HTTP ${res.status}`);
    }
    let paper = null;
    await streamSSE(res, (evt) => {
      if (evt.type === 'progress') {
        const mb = (evt.done / 1048576).toFixed(1);
        if (evt.total) {
          const pct = Math.min(99, (evt.done / evt.total) * 100);
          setProgress(pct, `下载中 ${mb} / ${(evt.total / 1048576).toFixed(1)} MB`);
          overlay(`正在下载 PDF… ${mb} MB`);
        } else {
          setProgress(50, `下载中 ${mb} MB`);
          overlay(`正在下载 PDF… ${mb} MB`);
        }
      } else if (evt.type === 'done') {
        paper = evt.paper;
      } else if (evt.type === 'error') {
        throw new Error(evt.message);
      }
    });
    if (!paper) throw new Error('下载未完成');
    hideProgress();
    toast(paper.readable === false ? 'PDF 无文本层（扫描件），翻译与检索不可用' : '已加入书库');
    await openPaper(paper.id);
  } catch (err) {
    hideProgress();
    toast('打开失败：' + err.message, true);
  } finally {
    overlay(null);
  }
}

async function uploadFiles(files) {
  const pdfs = Array.from(files).filter((f) => /\.pdf$/i.test(f.name));
  if (!pdfs.length) return toast('请选择 PDF 文件', true);
  overlay(`正在导入 ${pdfs.length} 个文件…`);
  let last = null;
  try {
    for (const file of pdfs) {
      overlay(`正在导入 ${file.name}…`);
      const form = new FormData();
      form.append('file', file);
      const data = await api('/api/papers/upload', { method: 'POST', body: form });
      last = data.paper;
    }
    toast(`已导入 ${pdfs.length} 个文件`);
    await loadHistory();
    if (last) await openPaper(last.id);
  } catch (err) {
    toast('导入失败：' + err.message, true);
  } finally {
    overlay(null);
  }
}

/* ══════════════════════════ 打开论文 / 渲染页面 ══════════════════════════ */

async function openPaper(pid) {
  overlay('正在打开…');
  try {
    const data = await api(`/api/papers/${encodeURIComponent(pid)}`);
    state.paper = data.paper;
    const sizes = await api(`/api/papers/${encodeURIComponent(pid)}/pages`);

    $('#home').classList.add('hidden');
    $('#reader').classList.remove('hidden');
    $('#docTitle').textContent = data.paper.title || '未命名';
    $('#docTitle').title = data.paper.title || '';
    $('#pageTotal').textContent = `/ ${sizes.count}`;
    clearChatLog();
    $('#quoteBar').classList.add('hidden');
    state.selection = '';
    state.count = sizes.count;
    state.pageSizes = sizes.pages;
    state.rendered = new Set();
    state.translating = new Set();
    state.layout = new Map();
    state.zoom = 1;
    $('#zoomLabel').textContent = '100%';

    buildPageShells();
    renderMessages(data.messages || []);
    computeFit();
    drawAllPages();

    const start = Math.min(data.paper.last_page || 0, Math.max(0, sizes.count - 1));
    if (start > 0) requestAnimationFrame(() => gotoPage(start, false));
    $('#pageInput').value = String(start + 1);
  } catch (err) {
    toast('打开失败：' + err.message, true);
  } finally {
    overlay(null);
  }
}

function buildPageShells() {
  const stack = $('#pageStack');
  stack.innerHTML = '';
  state.pageSizes.forEach((size, n) => {
    const div = document.createElement('div');
    div.className = 'page';
    div.dataset.page = String(n);
    const img = document.createElement('img');
    img.className = 'page-img';
    img.alt = `第 ${n + 1} 页`;
    img.loading = 'lazy';
    div.appendChild(img);
    const num = document.createElement('div');
    num.className = 'page-num';
    num.textContent = String(n + 1);
    div.appendChild(num);
    stack.appendChild(div);
  });
}

/** fitScale = 每个 PDF 点对应多少 CSS 像素。两种取景方式：
 *  width → 铺满栏宽（默认，字大，需要纵向滚动）
 *  page  → 整页塞进可视区（一屏看全，字小） */
function computeFit() {
  const scroll = $('#pageScroll');
  const availW = Math.max(120, scroll.clientWidth - 44);
  const availH = Math.max(120, scroll.clientHeight - 44);
  const maxW = Math.max(...state.pageSizes.map((s) => s.w), 1);
  const maxH = Math.max(...state.pageSizes.map((s) => s.h), 1);
  state.fitScale = state.fitMode === 'page'
    ? Math.min(availW / maxW, availH / maxH)
    : Math.min(availW / maxW, 2.2);
}

function applyFitModeUI() {
  const page = state.fitMode === 'page';
  $('#fitWidthBtn').style.color = page ? '' : 'var(--accent)';
  $('#fitPageBtn').style.color = page ? 'var(--accent)' : '';
}

function setFitMode(mode) {
  state.fitMode = mode;
  localStorage.setItem('pr-fit-mode', mode);
  applyFitModeUI();
  state.zoom = 1;
  $('#zoomLabel').textContent = '100%';
  if (state.paper) { computeFit(); drawAllPages(); }
}

function currentScale() { return state.fitScale * state.zoom; }

function drawAllPages() {
  const scale = currentScale();
  state.pageSizes.forEach((size, n) => {
    const el = $('#pageStack').children[n];
    el.style.width = `${size.w * scale}px`;
    el.style.height = `${size.h * scale}px`;
  });

  // 比例变了，已构建的两层需要重建；布局数据直接复用缓存，不重新请求。
  for (const n of Array.from(state.rendered)) {
    const el = $('#pageStack').children[n];
    if (!el) { state.rendered.delete(n); continue; }
    el.querySelector('.text-layer')?.remove();
    el.querySelector('.trans-layer')?.remove();
    el.classList.remove('loading');
    const layout = state.layout.get(n);
    if (layout) {
      buildTextLayer(el, layout, scale);
      buildTransLayer(el, layout, scale);
    } else {
      state.rendered.delete(n);
      ensurePageContent(n);
    }
  }
  applyLang();
  updateTranslateStatus();
  updateVisiblePages();
}

function ensurePageContent(n) {
  if (state.rendered.has(n) || n < 0 || n >= state.count) return;
  state.rendered.add(n);
  const el = $('#pageStack').children[n];
  const size = state.pageSizes[n];
  const scale = currentScale();

  const img = el.querySelector('.page-img');
  const zoomParam = Math.min(4, Math.max(1, scale * 2));
  img.src = `/api/papers/${encodeURIComponent(state.paper.id)}/pages/${n}.png?zoom=${zoomParam.toFixed(2)}`;

  api(`/api/papers/${encodeURIComponent(state.paper.id)}/pages/${n}/layout`)
    .then((layout) => {
      state.layout.set(n, layout);
      if (!state.paper.readable && !layout.has_text) {
        $('#viewerStatus').textContent = '扫描件：无文本层';
      }
      buildTextLayer(el, layout, scale);
      buildTransLayer(el, layout, scale);
      applyLang();
      if (state.lang === 'zh') requestTranslate(n);
    })
    .catch((err) => { el.classList.remove('loading'); console.warn(err); });
}

/** 每个单词一个绝对定位的透明 span，与页面图片像素级对齐，供原生选中使用。 */
function buildTextLayer(pageEl, layout, scale) {
  const layer = document.createElement('div');
  layer.className = 'text-layer';
  const parts = [];
  for (const w of layout.words) {
    const width = (w.x1 - w.x0) * scale;
    const height = (w.y1 - w.y0) * scale;
    if (width <= 0.5 || height <= 0.5) continue;
    const byHeight = height * 0.92;
    const byWidth = width / Math.max(1, w.t.length * 0.52);
    const fontSize = Math.max(1, Math.min(byHeight, byWidth));
    parts.push(
      `<span data-b="${w.b}" data-l="${w.l}" style="left:${(w.x0 * scale).toFixed(2)}px;`
      + `top:${(w.y0 * scale).toFixed(2)}px;width:${width.toFixed(2)}px;height:${height.toFixed(2)}px;`
      + `font-size:${fontSize.toFixed(2)}px;line-height:${height.toFixed(2)}px">${escapeHtml(w.t)}</span>`
    );
  }
  layer.innerHTML = parts.join('');
  pageEl.appendChild(layer);
}

/** 中文译文层：段落级白底块覆盖在淡化的原页上。 */
function buildTransLayer(pageEl, layout, scale) {
  const layer = document.createElement('div');
  layer.className = 'trans-layer';
  const parts = (layout.blocks || []).map((b) => blockHtml(b, scale));
  layer.innerHTML = parts.join('');
  pageEl.appendChild(layer);
}

function blockHtml(b, scale) {
  const style = `left:${(b.x0 * scale).toFixed(2)}px;top:${(b.y0 * scale).toFixed(2)}px;`
    + `width:${Math.max(30, (b.x1 - b.x0) * scale).toFixed(2)}px;`
    + `min-height:${((b.y1 - b.y0) * scale).toFixed(2)}px;`
    + `font-size:${Math.max(6, b.size * scale).toFixed(2)}px;line-height:1.5;z-index:${b.i}`;
  if (b.zh) return `<div class="block-zh" data-i="${b.i}" style="${style}">${escapeHtml(b.zh)}</div>`;
  return `<div class="block-zh pending" data-i="${b.i}" style="${style}"></div>`;
}

const pageObserver = new IntersectionObserver((entries) => {
  for (const entry of entries) {
    if (!entry.isIntersecting) continue;
    const n = Number(entry.target.dataset.page);
    ensurePageContent(n);
    if (state.lang === 'zh' && $('#autoTranslate').checked) requestTranslate(n);
  }
}, { root: $('#pageScroll'), rootMargin: '700px 0px' });

function updateVisiblePages(rebuild = false) {
  $$('#pageStack .page').forEach((el) => pageObserver.unobserve(el));
  $$('#pageStack .page').forEach((el) => pageObserver.observe(el));
  schedulePageNumberUpdate();
}

function schedulePageNumberUpdate() {
  const scroll = $('#pageScroll');
  const stackTop = $('#pageStack').offsetTop;
  let best = 0, bestDist = Infinity;
  state.pageSizes.forEach((size, n) => {
    const el = $('#pageStack').children[n];
    if (!el) return;
    const top = el.offsetTop - stackTop;
    const mid = top + el.offsetHeight / 2;
    const dist = Math.abs(mid - scroll.scrollTop - scroll.clientHeight / 2);
    if (dist < bestDist) { bestDist = dist; best = n; }
  });
  if (best !== state.currentPage) {
    state.currentPage = best;
    $('#pageInput').value = String(best + 1);
    saveProgress(best);
  }
}

let progressTimer = null;
function saveProgress(page) {
  if (!state.paper) return;
  clearTimeout(progressTimer);
  progressTimer = setTimeout(() => {
    api(`/api/papers/${encodeURIComponent(state.paper.id)}/progress`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ page }),
    }).catch(() => {});
  }, 700);
}

function gotoPage(n, smooth = true) {
  const el = $('#pageStack').children[n];
  if (!el) return;
  const scroll = $('#pageScroll');
  scroll.scrollTo({
    top: el.offsetTop - $('#pageStack').offsetTop - 10,
    behavior: smooth ? 'smooth' : 'auto',
  });
}

/* ══════════════════════════ 翻译 ══════════════════════════ */

function setLang(lang) {
  state.lang = lang;
  $$('#langToggle button').forEach((b) => b.classList.toggle('active', b.dataset.lang === lang));
  applyLang();
  if (lang === 'zh') {
    for (let n = 0; n < state.count; n++) {
      if (state.rendered.has(n)) requestTranslate(n);
    }
  }
}

function applyLang() {
  const zh = state.lang === 'zh';
  $$('#pageStack .page').forEach((el) => el.classList.toggle('tr-mode', zh));
}

/* 自动翻译会随滚动同时命中好几页。全局限制同时进行的页数，避免把模型端和
   本机连接数一次性打满；页内本身仍是 6 路并发。 */
const TRANSLATE_CONCURRENCY = 2;
const translateQueue = [];
const translatePending = new Map();   // page -> Promise

function requestTranslate(n, force = false) {
  if (translatePending.has(n)) return translatePending.get(n);
  const promise = new Promise((resolve) => {
    translateQueue.push({ n, force, resolve });
    pumpTranslateQueue();
  });
  translatePending.set(n, promise);
  return promise;
}

function pumpTranslateQueue() {
  while (state.translating.size < TRANSLATE_CONCURRENCY && translateQueue.length) {
    const job = translateQueue.shift();
    translatePage(job.n, job.force)
      .catch(() => {})
      .finally(() => {
        translatePending.delete(job.n);
        job.resolve();
        pumpTranslateQueue();
      });
  }
}

async function translatePage(n, force = false) {
  if (state.lang !== 'zh') return;
  const pageEl = $('#pageStack').children[n];
  if (!pageEl || !state.layout.has(n)) return;
  const layout = state.layout.get(n);
  if (!layout.blocks || !layout.blocks.length) return;

  // 已全部翻译过就不重复请求（除非显式要求重译）
  if (!force && layout.blocks.every((b) => b.zh)) {
    updateTranslateStatus();
    return;
  }

  state.translating.add(n);
  updateTranslateStatus();
  const url = `/api/papers/${encodeURIComponent(state.paper.id)}/pages/${n}/translate`
    + (force ? '?force=true' : '');
  try {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    await streamSSE(res, (evt) => {
      if (evt.type === 'block') {
        const block = layout.blocks.find((b) => b.i === evt.i);
        if (block && !block.zh) block.zh = evt.zh;
        const node = pageEl.querySelector(`.block-zh[data-i="${evt.i}"]`);
        if (node) {
          node.classList.remove('pending', 'failed');
          if (evt.error) {
            node.classList.add('failed');
            node.textContent = '翻译失败：' + evt.error;
          } else {
            node.textContent = evt.zh || '';
          }
        }
      } else if (evt.type === 'page_done') {
        state.translating.delete(n);
        updateTranslateStatus();
      }
    });
  } catch (err) {
    console.warn('translate failed', err);
    toast(`第 ${n + 1} 页翻译失败：${err.message}`, true);
  } finally {
    state.translating.delete(n);
    updateTranslateStatus();
  }
}

function updateTranslateStatus() {
  const busy = state.translating.size;
  const total = state.count;
  const done = Array.from({ length: total }, (_, n) => n).filter((n) => {
    const layout = state.layout.get(n);
    return layout && layout.blocks && layout.blocks.length && layout.blocks.every((b) => b.zh);
  }).length;
  $('#viewerStatus').textContent = state.lang === 'zh'
    ? (busy ? `翻译中… ${done}/${total} 页` : `已翻译 ${done}/${total} 页`)
    : (state.paper && state.paper.readable === false ? '扫描件：无文本层' : '');
}

async function translateAll() {
  // setLang('zh') 会把已渲染的页排进翻译队列；
  // 其余页面先拉取段落结构，布局到位的瞬间会被 ensurePageContent 自动排进队列。
  setLang('zh');
  toast('开始翻译全文，已翻译过的段落会直接命中缓存');
  for (let n = 0; n < state.count; n++) ensurePageContent(n);
}

/* ══════════════════════════ 选中 → 提问 ══════════════════════════ */

/** 从选区重建干净文本：同行的词用空格连接，段落之间空行。
 *
 * 用 range.intersectsNode() 而不是 cloneContents()：克隆出来的 fragment 只保留
 * 选区本身，不含 .text-layer 祖先，选择器会一个都匹配不到（退化成没有空格的
 * 原始字符串）。逐节点判定同时天然保持了阅读顺序。
 */
function extractSelectionText(sel) {
  const range = sel.getRangeAt(0);
  const scope = $('#pageStack') || document;

  const zhNodes = [];
  scope.querySelectorAll('.block-zh').forEach((el) => {
    if (!el.classList.contains('pending') && range.intersectsNode(el)) {
      const t = el.textContent.trim();
      if (t) zhNodes.push(t);
    }
  });
  if (zhNodes.length) return zhNodes.join('\n\n');

  const blocks = new Map();   // block -> Map(line -> words[])
  scope.querySelectorAll('.text-layer span[data-b]').forEach((s) => {
    if (!range.intersectsNode(s)) return;
    const b = s.dataset.b, l = s.dataset.l;
    if (!blocks.has(b)) blocks.set(b, new Map());
    const lines = blocks.get(b);
    if (!lines.has(l)) lines.set(l, []);
    lines.get(l).push(s.textContent);
  });
  if (blocks.size) {
    return Array.from(blocks.values()).map((lines) =>
      Array.from(lines.values()).map((words) => words.join(' ')).join(' ')
    ).join('\n\n');
  }

  return sel.toString().replace(/\s*\n\s*/g, ' ').replace(/\s{2,}/g, ' ').trim();
}

const selMenu = $('#selectionMenu');
let pendingSelection = '';

function hideSelMenu() { selMenu.classList.add('hidden'); }

function onMouseUp(e) {
  if (e.target.closest('#chatPane, #selectionMenu, .modal')) return;
  setTimeout(() => {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return hideSelMenu();
    const anchor = sel.anchorNode;
    const host = anchor && (anchor.nodeType === 1 ? anchor : anchor.parentElement);
    if (!host || !host.closest('#pageStack')) return hideSelMenu();

    const text = extractSelectionText(sel);
    if (!text || text.trim().length < 2) return hideSelMenu();

    pendingSelection = text.trim();
    const rect = sel.getRangeAt(0).getBoundingClientRect();
    const menuW = 260;
    let left = rect.left + rect.width / 2 - menuW / 2;
    left = Math.max(10, Math.min(left, window.innerWidth - menuW - 10));
    let top = rect.top - 46;
    if (top < 8) top = rect.bottom + 10;
    selMenu.style.left = `${left}px`;
    selMenu.style.top = `${top}px`;
    selMenu.classList.remove('hidden');
  }, 0);
}

selMenu.addEventListener('mousedown', (e) => e.preventDefault());
selMenu.addEventListener('click', (e) => {
  const act = e.target.dataset.act;
  if (!act) return;
  hideSelMenu();
  const text = pendingSelection;
  if (!text) return;
  setQuote(text);
  if (act === 'explain') sendMessage('请解释这段内容的含义，并说明它在论文中的作用。');
  else if (act === 'translate') sendMessage('请把这段内容翻译成通顺的中文。');
  else $('#chatInput').focus();
});

document.addEventListener('mouseup', onMouseUp);
document.addEventListener('mousedown', (e) => {
  if (!e.target.closest('#selectionMenu')) hideSelMenu();
});
document.addEventListener('copy', (e) => {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed) return;
  const anchor = sel.anchorNode;
  const host = anchor && (anchor.nodeType === 1 ? anchor : anchor.parentElement);
  if (!host || !host.closest('#pageStack')) return;
  const text = extractSelectionText(sel);
  if (text) e.clipboardData.setData('text/plain', text);
});

function setQuote(text) {
  state.selection = text;
  $('#quoteText').textContent = text.length > 600 ? text.slice(0, 600) + '…' : text;
  $('#quoteBar').classList.remove('hidden');
}

$('#quoteClear').addEventListener('click', () => {
  state.selection = '';
  $('#quoteBar').classList.add('hidden');
});

/* ══════════════════════════ 对话 ══════════════════════════ */

/** 只删消息节点：#chatEmpty 是 #chatLog 的子元素，整体清空会把它一起干掉。 */
function clearChatLog() {
  $$('#chatLog .msg').forEach((m) => m.remove());
  const empty = $('#chatEmpty');
  if (empty) empty.classList.remove('hidden');
}

function hideChatEmpty() {
  const empty = $('#chatEmpty');
  if (empty) empty.classList.add('hidden');
}

function renderMessages(messages) {
  if (!messages.length) { clearChatLog(); return; }
  hideChatEmpty();
  for (const m of messages) {
    const node = addMessageNode(m.role, m.content, m.context);
    node.dataset.persisted = '1';
  }
  scrollChatToBottom();
}

function addMessageNode(role, content, quote) {
  const wrap = document.createElement('div');
  wrap.className = `msg ${role}`;
  const label = role === 'user' ? '你' : 'AI 助手';
  wrap.innerHTML = `<div class="msg-head"><span class="msg-role">${label}</span></div>`;
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  if (quote) {
    const q = document.createElement('div');
    q.className = 'quote-inline';
    q.textContent = quote;
    bubble.appendChild(q);
  }
  const body = document.createElement('div');
  body.className = 'bubble-body';
  if (role === 'user') body.textContent = content;
  else body.innerHTML = mdToHtml(content);
  bubble.appendChild(body);
  wrap.appendChild(bubble);
  $('#chatLog').appendChild(wrap);
  hideChatEmpty();
  return wrap;
}

function scrollChatToBottom() {
  const log = $('#chatLog');
  log.scrollTop = log.scrollHeight;
}

async function sendMessage(text) {
  if (state.streaming) return toast('正在回答中，请稍候…', true);
  const message = (text !== undefined ? text : $('#chatInput').value).trim();
  if (!message) return;
  if (!state.paper) return toast('请先打开一篇论文', true);

  $('#chatInput').value = '';
  autoGrow($('#chatInput'));

  const quote = state.selection;
  state.selection = '';
  $('#quoteBar').classList.add('hidden');

  addMessageNode('user', message, quote);
  const assistant = addMessageNode('assistant', '');
  const body = assistant.querySelector('.bubble-body');
  body.classList.add('cursor-blink');
  scrollChatToBottom();

  let thinkingEl = null;
  let answer = '';
  state.streaming = true;
  $('#sendBtn').disabled = true;

  try {
    const res = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        paper_id: state.paper.id,
        message,
        page: state.currentPage || 0,
        selection: quote || '',
        model: $('#modelSelect').value || null,
        use_full_text: $('#fullTextToggle').checked,
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || `HTTP ${res.status}`);
    }

    await streamSSE(res, (evt) => {
      if (evt.type === 'thinking') {
        if (!thinkingEl) {
          thinkingEl = document.createElement('details');
          thinkingEl.className = 'thinking';
          thinkingEl.innerHTML = '<summary>思考中…（点击展开）</summary>';
          assistant.querySelector('.bubble').insertBefore(thinkingEl, body);
          if (state.lastThinkingOpen === true) thinkingEl.open = true;
        }
        thinkingEl.appendChild(document.createTextNode(evt.text));
        scrollChatToBottom();
      } else if (evt.type === 'text') {
        answer += evt.text;
        body.innerHTML = mdToHtml(answer);
        scrollChatToBottom();
      } else if (evt.type === 'error') {
        throw new Error(evt.message);
      } else if (evt.type === 'done') {
        if (thinkingEl) {
          state.lastThinkingOpen = thinkingEl.open;
          thinkingEl.querySelector('summary').textContent = '思考过程（点击展开）';
        }
      }
    });

    if (!answer) body.innerHTML = '<p class="muted">（模型没有返回内容）</p>';
  } catch (err) {
    body.innerHTML = `<p style="color:var(--err)">请求失败：${escapeHtml(err.message)}</p>`;
  } finally {
    body.classList.remove('cursor-blink');
    state.streaming = false;
    $('#sendBtn').disabled = false;
    scrollChatToBottom();
  }
}

function autoGrow(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 190) + 'px';
}

/* ══════════════════════════ 设置 ══════════════════════════ */

async function loadHealth() {
  try {
    const data = await api('/api/health');
    const llm = data.llm;
    state.models = llm.models || [];
    const badge = $('#llmBadge');
    badge.textContent = llm.configured ? `${llm.model}` : '未配置模型';
    badge.className = 'badge ' + (llm.configured ? 'ok' : 'bad');
    badge.title = llm.configured
      ? `${llm.model} @ ${llm.base_url}\n密钥：${llm.secret_hint}`
      : '未检测到大模型配置，点击「设置」填写';
    const sel = $('#modelSelect');
    const current = llm.model;
    const options = Array.from(new Set([current, ...state.models].filter(Boolean)));
    sel.innerHTML = options.map((m) => `<option value="${escapeHtml(m)}">${escapeHtml(m)}</option>`).join('');
    $('#modelList').innerHTML = options.map((m) => `<option value="${escapeHtml(m)}"></option>`).join('');
  } catch (err) {
    $('#llmBadge').textContent = '服务异常';
    $('#llmBadge').className = 'badge bad';
  }
}

async function openSettings() {
  try {
    const llm = await api('/api/settings');
    $('#setBaseUrl').value = llm.base_url || '';
    $('#setModel').value = llm.model || '';
    $('#setProtocol').value = llm.protocol || 'anthropic';
    $('#setSecret').value = '';
    $('#setSecret').placeholder = llm.configured ? `已配置（${llm.secret_hint}），留空则不修改` : 'sk-…';
    $('#settingsResult').className = 'settings-result';
  } catch (_) { /* 用默认空表单 */ }
  $('#settingsModal').classList.remove('hidden');
}

async function saveSettings() {
  const patch = {
    base_url: $('#setBaseUrl').value.trim() || null,
    model: $('#setModel').value.trim() || null,
    protocol: $('#setProtocol').value,
    supports_thinking_toggle: $('#setThinking').checked,
  };
  const secret = $('#setSecret').value.trim();
  if (secret) {
    if ($('#setProtocol').value === 'openai') patch.api_key = secret;
    else patch.auth_token = secret;
  }
  try {
    await api('/api/settings', {
      method: 'PUT',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(patch),
    });
    toast('设置已保存');
    $('#settingsModal').classList.add('hidden');
    await loadHealth();
  } catch (err) {
    toast('保存失败：' + err.message, true);
  }
}

async function testLlm() {
  const box = $('#settingsResult');
  box.className = 'settings-result show';
  box.textContent = '测试中…';
  try {
    const r = await api('/api/settings/test', { method: 'POST' });
    if (r.ok) {
      box.classList.add('ok');
      box.textContent = `✓ 连接成功\n模型：${r.model}\n回复：${r.reply}`;
    } else {
      box.classList.add('bad');
      box.textContent = `✗ ${r.error}`;
    }
  } catch (err) {
    box.classList.add('bad');
    box.textContent = '✗ ' + err.message;
  }
}

/* ══════════════════════════ 布局交互 ══════════════════════════ */

function setFocus(mode) {
  state.focus = state.focus === mode ? null : mode;
  const reader = $('#reader');
  reader.classList.toggle('focus-left', state.focus === 'left');
  reader.classList.toggle('focus-right', state.focus === 'right');
  $('#focusLeftBtn').title = state.focus === 'left' ? '退出全屏阅读' : '全屏阅读（折叠对话）';
  $('#focusRightBtn').title = state.focus === 'right' ? '退出全屏对话' : '全屏对话（折叠论文）';
  setTimeout(() => { computeFit(); drawAllPages(); }, 60);
}

function initSplitter() {
  const saved = parseFloat(localStorage.getItem('pr-left-w'));
  if (saved > 20 && saved < 85) document.documentElement.style.setProperty('--left-w', `${saved}%`);
  const splitter = $('#splitter');
  let dragging = false;
  splitter.addEventListener('mousedown', (e) => {
    dragging = true;
    splitter.classList.add('dragging');
    document.body.style.userSelect = 'none';
    document.body.style.cursor = 'col-resize';
    e.preventDefault();
  });
  window.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    const rect = $('#reader').getBoundingClientRect();
    const pct = Math.min(85, Math.max(20, ((e.clientX - rect.left) / rect.width) * 100));
    document.documentElement.style.setProperty('--left-w', `${pct}%`);
  });
  window.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    splitter.classList.remove('dragging');
    document.body.style.userSelect = '';
    document.body.style.cursor = '';
    const pct = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--left-w'));
    localStorage.setItem('pr-left-w', String(pct));
    computeFit();
    drawAllPages();
  });
}

function showHome() {
  $('#reader').classList.add('hidden');
  $('#home').classList.remove('hidden');
  state.paper = null;
  loadHistory($('#historyFilter').value.trim());
}

/* ══════════════════════════ 事件绑定 ══════════════════════════ */

function bind() {
  // 顶栏
  $('#brandBtn').addEventListener('click', showHome);
  $('#libraryBtn').addEventListener('click', showHome);
  $('#settingsBtn').addEventListener('click', openSettings);
  $('#quickSearchBtn').addEventListener('click', () => {
    const q = $('#quickSearch').value.trim();
    if (q) { $('#homeSearch').value = q; showHome(); doSearch(q, $('#sourceSelect').value); }
  });
  $('#quickSearch').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('#quickSearchBtn').click(); });
  $('#openLocalBtn').addEventListener('click', () => $('#fileInput').click());
  $('#dropBrowseBtn').addEventListener('click', () => $('#fileInput').click());
  $('#fileInput').addEventListener('change', (e) => { uploadFiles(e.target.files); e.target.value = ''; });
  $('#pathModalBtn').addEventListener('click', () => $('#pathModal').classList.remove('hidden'));
  $('#pathOpenBtn').addEventListener('click', async () => {
    const path = $('#pathInput').value.trim();
    if (!path) return;
    overlay('正在打开…');
    try {
      const data = await api('/api/papers/open-local', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ path }),
      });
      $('#pathModal').classList.add('hidden');
      $('#pathInput').value = '';
      await openPaper(data.paper.id);
    } catch (err) {
      toast('打开失败：' + err.message, true);
    } finally {
      overlay(null);
    }
  });
  $('#pathInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('#pathOpenBtn').click(); });

  // 首页
  $('#homeSearchBtn').addEventListener('click', () => doSearch($('#homeSearch').value.trim(), $('#sourceSelect').value));
  $('#homeSearch').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('#homeSearchBtn').click(); });
  $('#historyFilter').addEventListener('input', () => {
    clearTimeout(state.histTimer);
    state.histTimer = setTimeout(() => loadHistory($('#historyFilter').value.trim()), 260);
  });

  // 拖拽
  const dz = $('#dropZone');
  ['dragenter', 'dragover'].forEach((ev) => dz.addEventListener(ev, (e) => {
    e.preventDefault(); dz.classList.add('over');
  }));
  ['dragleave', 'drop'].forEach((ev) => dz.addEventListener(ev, (e) => {
    e.preventDefault(); dz.classList.remove('over');
  }));
  dz.addEventListener('drop', (e) => { if (e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files); });
  window.addEventListener('dragover', (e) => e.preventDefault());
  window.addEventListener('drop', (e) => {
    e.preventDefault();
    if (e.dataTransfer.files.length && !e.target.closest('#dropZone')) uploadFiles(e.dataTransfer.files);
  });

  // 阅读器工具条
  $('#backBtn').addEventListener('click', showHome);
  $('#prevBtn').addEventListener('click', () => gotoPage(Math.max(0, (state.currentPage || 0) - 1)));
  $('#nextBtn').addEventListener('click', () => gotoPage(Math.min(state.count - 1, (state.currentPage || 0) + 1)));
  $('#pageInput').addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    const n = parseInt(e.target.value, 10);
    if (!isNaN(n)) gotoPage(Math.min(state.count - 1, Math.max(0, n - 1)));
    e.target.blur();
  });
  $('#zoomInBtn').addEventListener('click', () => setZoom(state.zoom * 1.2));
  $('#zoomOutBtn').addEventListener('click', () => setZoom(state.zoom / 1.2));
  $('#fitWidthBtn').addEventListener('click', () => setFitMode('width'));
  $('#fitPageBtn').addEventListener('click', () => setFitMode('page'));
  $('#focusLeftBtn').addEventListener('click', () => setFocus('left'));
  $('#focusRightBtn').addEventListener('click', () => setFocus('right'));
  $('#langToggle').addEventListener('click', (e) => {
    const lang = e.target.dataset.lang;
    if (lang) setLang(lang);
  });
  $('#translatePageBtn').addEventListener('click', () => {
    setLang('zh');
    requestTranslate(state.currentPage || 0);
  });
  $('#translateAllBtn').addEventListener('click', translateAll);
  $('#pageScroll').addEventListener('scroll', () => {
    clearTimeout(state.scrollTimer);
    state.scrollTimer = setTimeout(() => schedulePageNumberUpdate(), 80);
  });

  // 对话
  $('#sendBtn').addEventListener('click', () => sendMessage());
  $('#chatInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendMessage(); }
  });
  $('#chatInput').addEventListener('input', (e) => autoGrow(e.target));
  $('#clearChatBtn').addEventListener('click', async () => {
    if (!state.paper || !confirm('清空这篇论文的对话记录？')) return;
    await api(`/api/papers/${encodeURIComponent(state.paper.id)}/messages/clear`, { method: 'POST' });
    clearChatLog();
  });
  $('#chatLog').addEventListener('click', (e) => {
    const chip = e.target.closest('.chip[data-q]');
    if (chip) sendMessage(chip.dataset.q);
  });

  // 弹窗
  $('#saveSettingsBtn').addEventListener('click', saveSettings);
  $('#testLlmBtn').addEventListener('click', testLlm);
  $$('[data-close]').forEach((b) => b.addEventListener('click', () => {
    $('#' + b.dataset.close).classList.add('hidden');
  }));
  $$('.modal').forEach((m) => m.addEventListener('click', (e) => {
    if (e.target === m) m.classList.add('hidden');
  }));

  // 快捷键
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      hideSelMenu();
      if (state.focus) setFocus(state.focus);
      $$('.modal').forEach((m) => m.classList.add('hidden'));
      return;
    }
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || '');
    if (typing || !state.paper) return;
    if (e.key === 'ArrowLeft' || e.key === 'PageUp') { e.preventDefault(); $('#prevBtn').click(); }
    if (e.key === 'ArrowRight' || e.key === 'PageDown') { e.preventDefault(); $('#nextBtn').click(); }
    if (e.key === 'f') setFocus('right');
    if (e.key === 'F') setFocus('left');
  });

  window.addEventListener('resize', () => {
    if (!state.paper) return;
    clearTimeout(state.resizeTimer);
    state.resizeTimer = setTimeout(() => { computeFit(); drawAllPages(); }, 180);
  });
}

function setZoom(zoom) {
  state.zoom = Math.min(4, Math.max(0.4, zoom));
  $('#zoomLabel').textContent = `${Math.round(state.zoom * 100)}%`;
  drawAllPages();
}

/* ══════════════════════════ 启动 ══════════════════════════ */

(function init() {
  state.currentPage = 0;
  state.fitMode = localStorage.getItem('pr-fit-mode') === 'page' ? 'page' : 'width';
  applyFitModeUI();
  bind();
  initSplitter();
  loadHealth();
  loadHistory();
})();
