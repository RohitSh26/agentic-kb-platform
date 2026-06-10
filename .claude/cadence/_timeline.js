/**
 * Timeline renderer · iter 2.
 *
 * Markup matches Claude Design's iter-2 mocks (mocks/01-single-linear.html
 * and mocks/06-graph-blocks.html). Compact-when-closed cards, stats ribbon,
 * failure banner, view-mode toggle, full chart-block vocabulary.
 */
(function () {
  'use strict';

  const AGENT_LANE = {
    'orchestrator':   'var(--lane-trunk)',
    'founder':        'var(--apricot-600)',
    'ui-agent':       'var(--lane-success)',
    'backend-agent':  'var(--lane-info)',
    'infra-agent':    'var(--lane-success)',
    'eval-agent':     'var(--lane-warning)',
    'external':       'var(--lane-warning)',
  };

  const STATUS_LABEL = {
    'in_progress': 'in-progress',
    'completed':   'completed',
    'failed':      'failed',
    'blocked':     'blocked',
    'decision':    'decision',
  };

  const STATUS_COLOR = {
    'in_progress': 'var(--info)',
    'completed':   'var(--success)',
    'failed':      'var(--danger)',
    'blocked':     'var(--warning)',
    'decision':    'var(--apricot-600)',
  };

  let activeStatus  = 'all';
  let activeAgent   = 'all';
  let activeSession = 'all';      // v1.3: session filter
  let activeMode    = 'full';      // full · summary · compact · events
  let searchQuery   = '';
  let activeSessionForView = null; // v1.5: which session is in the detail pane
  let activeTimeRange = 'all';     // v1.7: 'all' | '1h' | '24h' | '7d' | 'custom'
  let customFrom = '';             // ISO local string, used when range = custom
  let customTo   = '';
  const collapsedGroups = new Set();          // v1.8: which sidebar status groups are collapsed
  const expandedChildGroups = new Set();      // v1.8: which (turn:tool) child groups are expanded
  const expandedShowAll     = new Set();      // v1.8: which (turn:tool) child groups have "show all"

  const TIME_PRESETS_MS = {
    '1h':  60 * 60 * 1000,
    '24h': 24 * 60 * 60 * 1000,
    '7d':  7 * 24 * 60 * 60 * 1000,
  };

  function timeBoundsForFilter() {
    if (activeTimeRange === 'all') return null;
    if (activeTimeRange === 'custom') {
      return {
        from: customFrom ? new Date(customFrom).getTime() : null,
        to:   customTo   ? new Date(customTo).getTime()   : null,
      };
    }
    const ms = TIME_PRESETS_MS[activeTimeRange];
    if (!ms) return null;
    return { from: Date.now() - ms, to: null };
  }

  // ─────────── DOM helpers ───────────
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function laneVar(agent) { return AGENT_LANE[agent] || 'var(--lane-trunk)'; }
  function escapeHtml(s) {
    return String(s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }
  function chevronSvg() {
    return '<svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.6"><path d="m4 2 4 4-4 4"/></svg>';
  }
  function iso(ts) {
    if (!ts) return '';
    const d = new Date(ts);
    if (isNaN(d)) return ts;
    return ts.replace('Z','').replace('T',' ').slice(0, 19) + 'Z';
  }
  function shortTime(ts) {
    if (!ts) return '';
    const d = new Date(ts);
    if (isNaN(d)) return ts;
    const pad = n => String(n).padStart(2, '0');
    return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`;
  }
  function rel(ts) {
    if (!ts) return '';
    const d = new Date(ts); if (isNaN(d)) return '';
    const s = Math.max(0, Math.floor((Date.now() - d.getTime()) / 1000));
    if (s < 60) return 'just now';
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h}h`;
    return `${Math.floor(h / 24)}d`;
  }
  // Block-aware markdown renderer. Handles ATX headings, fenced code,
  // blockquotes (recursive), ordered + unordered lists, horizontal rules,
  // and paragraphs. Inline: **bold**, *italic*, `code`, [text](url).
  // Deliberately small — no tables (use the `table` block) and no images.
  function safeHref(raw) {
    const trimmed = String(raw).trim();
    // Block javascript:, data:, vbscript: schemes — anything else is allowed.
    if (/^(javascript|data|vbscript):/i.test(trimmed)) return '#';
    return trimmed;
  }
  function inlineMd(s) {
    s = escapeHtml(s);
    s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>');
    s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g,
      (_m, label, href) => `<a href="${escapeHtml(safeHref(href))}" target="_blank" rel="noopener">${label}</a>`);
    return s;
  }
  // v2.1.1: detect a GFM-style pipe table starting at lines[i].
  //   | Header A | Header B |
  //   |----------|----------|
  //   | row 1 a  | row 1 b  |
  // Returns { html, consumed } if a valid table starts here, else null.
  // Per-cell alignment via ":---", ":--:", "---:" is honored.
  function parsePipeTable(lines, i) {
    const isPipeRow = (s) => /\|/.test(s) && s.trim().length > 0;
    const isSepRow = (s) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$/.test(s);
    if (i + 1 >= lines.length) return null;
    if (!isPipeRow(lines[i]) || !isSepRow(lines[i + 1])) return null;

    function splitRow(s) {
      // Strip leading + trailing pipe if present, then split on inner pipes.
      // Preserve escaped \| as a literal pipe within a cell.
      let t = s.trim();
      if (t.startsWith('|')) t = t.slice(1);
      if (t.endsWith('|'))   t = t.slice(0, -1);
      const cells = [];
      let cur = '', esc = false;
      for (const ch of t) {
        if (esc)               { cur += ch; esc = false; continue; }
        if (ch === '\\')       { esc = true; continue; }
        if (ch === '|')        { cells.push(cur.trim()); cur = ''; continue; }
        cur += ch;
      }
      cells.push(cur.trim());
      return cells;
    }

    const headers = splitRow(lines[i]);
    const aligns = splitRow(lines[i + 1]).map(c => {
      const left = c.startsWith(':');
      const right = c.endsWith(':');
      return right && left ? 'center' : right ? 'right' : left ? 'left' : '';
    });
    // header column count must match separator column count
    if (headers.length !== aligns.length) return null;

    const rows = [];
    let j = i + 2;
    while (j < lines.length && isPipeRow(lines[j]) && !isSepRow(lines[j])) {
      const cells = splitRow(lines[j]);
      while (cells.length < headers.length) cells.push('');
      cells.length = headers.length;
      rows.push(cells);
      j += 1;
    }

    let html = '<table>';
    html += '<thead><tr>';
    headers.forEach((h, k) => {
      const sty = aligns[k] ? ` style="text-align:${aligns[k]}"` : '';
      html += `<th${sty}>${inlineMd(h)}</th>`;
    });
    html += '</tr></thead><tbody>';
    rows.forEach(r => {
      html += '<tr>';
      r.forEach((c, k) => {
        const sty = aligns[k] ? ` style="text-align:${aligns[k]}"` : '';
        html += `<td${sty}>${inlineMd(c)}</td>`;
      });
      html += '</tr>';
    });
    html += '</tbody></table>';
    return { html, consumed: j - i };
  }

  function simpleMd(src) {
    const lines = String(src || '').split('\n');
    const out = [];
    const blockStart = /^(```|#{1,6}\s|>|[-*+]\s|\d+\.\s|---\s*$|\*\*\*\s*$|___\s*$|\|)/;
    let i = 0;
    while (i < lines.length) {
      const ln = lines[i];

      if (/^```/.test(ln)) {
        const lang = ln.replace(/^```/, '').trim();
        const buf = [];
        i += 1;
        while (i < lines.length && !/^```/.test(lines[i])) { buf.push(lines[i]); i += 1; }
        i += 1;
        out.push(
          '<pre' + (lang ? ` data-lang="${escapeHtml(lang)}"` : '') + '><code>' +
          escapeHtml(buf.join('\n')) +
          '</code></pre>'
        );
        continue;
      }

      // GFM pipe table — check before HR because the separator line "---" can
      // collide with table separators. parsePipeTable returns null fast if
      // the lookahead doesn't form a real table.
      const tbl = parsePipeTable(lines, i);
      if (tbl) {
        out.push(tbl.html);
        i += tbl.consumed;
        continue;
      }

      if (/^(---|\*\*\*|___)\s*$/.test(ln)) {
        out.push('<hr>');
        i += 1;
        continue;
      }

      const h = ln.match(/^(#{1,6})\s+(.+?)\s*#*\s*$/);
      if (h) {
        out.push(`<h${h[1].length}>${inlineMd(h[2])}</h${h[1].length}>`);
        i += 1;
        continue;
      }

      if (/^>/.test(ln)) {
        const bq = [];
        while (i < lines.length && /^>/.test(lines[i])) {
          bq.push(lines[i].replace(/^>\s?/, ''));
          i += 1;
        }
        out.push('<blockquote>' + simpleMd(bq.join('\n')) + '</blockquote>');
        continue;
      }

      if (/^[-*+]\s+/.test(ln)) {
        const items = [];
        while (i < lines.length && /^[-*+]\s+/.test(lines[i])) {
          items.push(lines[i].replace(/^[-*+]\s+/, ''));
          i += 1;
        }
        out.push('<ul>' + items.map(it => `<li>${inlineMd(it)}</li>`).join('') + '</ul>');
        continue;
      }

      if (/^\d+\.\s+/.test(ln)) {
        const items = [];
        while (i < lines.length && /^\d+\.\s+/.test(lines[i])) {
          items.push(lines[i].replace(/^\d+\.\s+/, ''));
          i += 1;
        }
        out.push('<ol>' + items.map(it => `<li>${inlineMd(it)}</li>`).join('') + '</ol>');
        continue;
      }

      if (!ln.trim()) { i += 1; continue; }

      const para = [];
      while (i < lines.length && lines[i].trim() && !blockStart.test(lines[i])) {
        para.push(lines[i]);
        i += 1;
      }
      if (para.length) out.push('<p>' + para.map(inlineMd).join('<br>') + '</p>');
    }
    return out.join('\n');
  }
  function fmtElapsed(ms) {
    const s = Math.max(0, Math.floor(ms / 1000));
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m ${s % 60}s`;
    const h = Math.floor(m / 60);
    return `${h}h ${m % 60}m`;
  }

  // ─────────── Block renderers ───────────
  const renderers = {
    markdown:     renderMarkdown,
    table:        renderTable,
    code:         renderCode,
    diagram:      renderDiagram,
    chart:        renderSparkline,           // legacy alias
    sparkline:    renderSparkline,
    bar:          renderBar,
    line:         renderLine,
    area:         renderArea,
    distribution: renderDistribution,
    status_grid:  renderStatusGrid,
    agent_heatmap:renderHeatmap,
    progress_arc: renderProgressArc,
    lane_flow:    renderLaneFlow,
    delta_bar:    renderDeltaBar,
    checklist:    renderChecklist,
    decision:     renderDecision,
    link:         renderLink,
    key_value:    renderKeyValue,
  };

  function blockLabel(text, meta) {
    const div = el('div','block-label');
    div.textContent = text;
    if (meta) {
      const m = el('span','meta',meta);
      div.appendChild(m);
    }
    return div;
  }

  function renderMarkdown(b) {
    const div = el('div','block md');
    if (b.label) div.appendChild(blockLabel(b.label));
    const body = el('div'); body.innerHTML = simpleMd(b.value || '');
    div.appendChild(body);
    return div;
  }
  function renderTable(b) {
    const div = el('div','block table');
    if (b.label) div.appendChild(blockLabel(b.label));
    let html = '<table>';
    if (b.headers) {
      html += '<thead><tr>';
      b.headers.forEach(h => html += `<th>${escapeHtml(h)}</th>`);
      html += '</tr></thead>';
    }
    html += '<tbody>';
    (b.rows || []).forEach(r => {
      html += '<tr>';
      r.forEach(c => html += `<td>${escapeHtml(c)}</td>`);
      html += '</tr>';
    });
    html += '</tbody></table>';
    const wrap = el('div'); wrap.innerHTML = html;
    div.appendChild(wrap.firstChild);
    return div;
  }
  function renderCode(b) {
    const div = el('div','block code');
    if (b.lang) {
      const t = el('div','lang-tag'); t.textContent = b.lang; div.appendChild(t);
    }
    const pre = el('pre'); pre.textContent = b.value || '';
    div.appendChild(pre);
    return div;
  }
  function renderDiagram(b) {
    const div = el('div','block diagram');
    if (b.label) div.appendChild(blockLabel(b.label));
    div.innerHTML += b.svg || '';
    return div;
  }
  function renderSparkline(b) {
    const div = el('div','block chart-block sparkline');
    const series = b.series || [];
    const max = Math.max(...series.map(p => p.y ?? p.value ?? 0), 1);
    const min = Math.min(...series.map(p => p.y ?? p.value ?? 0), 0);
    const w = 280, h = 44;
    const pts = series.map((p, i) => {
      const x = (i / Math.max(series.length - 1, 1)) * w;
      const v = p.y ?? p.value ?? 0;
      const y = h - ((v - min) / Math.max(max - min, 1)) * h;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(' ');
    let html = `<div class="stat"><span class="v tabular">${escapeHtml(b.kpi || '')}</span>`;
    if (b.label) html += `<span class="l">${escapeHtml(b.label)}</span>`;
    html += '</div>';
    html += `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">`;
    html += `<polyline fill="none" stroke="var(--mid)" stroke-width="1.4" points="${pts}"/>`;
    if (series.length) {
      const last = series[series.length - 1];
      const lv = last.y ?? last.value ?? 0;
      const ly = h - ((lv - min) / Math.max(max - min, 1)) * h;
      html += `<circle cx="${w}" cy="${ly.toFixed(1)}" r="2.6" fill="var(--apricot-500)"/>`;
    }
    html += `<line x1="0" y1="${h-2}" x2="${w}" y2="${h-2}" stroke="var(--soft)" stroke-width="0.5"/></svg>`;
    div.innerHTML = html;
    return div;
  }
  function renderBar(b) {
    const div = el('div','block chart-block bar');
    if (b.label) div.appendChild(blockLabel(b.label));
    const cats = b.categories || []; const vals = b.values || [];
    const max = Math.max(...vals, 1);
    const rows = el('div','rows');
    cats.forEach((c, i) => {
      const v = vals[i] || 0;
      const row = el('div','row');
      row.style.display = 'grid';
      row.style.gridTemplateColumns = '120px 1fr 50px';
      row.style.gap = '8px';
      row.style.alignItems = 'center';
      row.style.fontSize = '12px';
      row.style.padding = '4px 0';
      const lab = el('span','row-label'); lab.textContent = c;
      const track = el('div','row-track'); track.style.background = 'var(--bone)'; track.style.height = '8px'; track.style.borderRadius = '2px';
      const fill = el('div','row-fill'); fill.style.background = 'var(--apricot-500)'; fill.style.width = ((v/max)*100)+'%'; fill.style.height = '100%'; fill.style.borderRadius = '2px';
      track.appendChild(fill);
      const val = el('span','row-value tabular'); val.textContent = v + (b.unit ? ' '+b.unit : '');
      row.appendChild(lab); row.appendChild(track); row.appendChild(val);
      rows.appendChild(row);
    });
    div.appendChild(rows);
    return div;
  }
  function renderLine(b) {
    const div = el('div','block chart-block line');
    if (b.label) div.appendChild(blockLabel(b.label));
    const series = b.series || [];
    const w = 480, h = 120, pad = 8;
    let html = `<div class="plot" style="background:var(--paper);border:1px solid var(--soft);border-radius:6px;padding:10px;">`;
    html += `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" style="width:100%;height:120px;">`;
    series.forEach((s, idx) => {
      const vals = s.values || [];
      const max = Math.max(...vals, 1);
      const pts = vals.map((v, i) => {
        const x = pad + (i / Math.max(vals.length - 1, 1)) * (w - 2*pad);
        const y = h - pad - (v / max) * (h - 2*pad);
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      }).join(' ');
      const stroke = ['var(--apricot-600)','var(--info)','var(--success)'][idx] || 'var(--mid)';
      html += `<polyline fill="none" stroke="${stroke}" stroke-width="1.5" points="${pts}"/>`;
    });
    html += '</svg></div>';
    div.innerHTML += html;
    return div;
  }
  function renderArea(b) {
    const out = renderLine(b);
    out.classList.remove('line'); out.classList.add('area');
    return out;
  }
  function renderDistribution(b) {
    const div = el('div','block chart-block distribution');
    if (b.label) div.appendChild(blockLabel(b.label));
    const bins = b.bins || (b.raw ? bucketize(b.raw, 12) : []);
    const max = Math.max(...bins.map(x => x.count || 0), 1);
    let html = '<svg viewBox="0 0 320 100" preserveAspectRatio="none" style="width:100%;height:100px;">';
    const w = 320 / Math.max(bins.length, 1);
    bins.forEach((bin, i) => {
      const h = (bin.count / max) * 92;
      html += `<rect x="${(i*w + 1).toFixed(1)}" y="${(96-h).toFixed(1)}" width="${(w-2).toFixed(1)}" height="${h.toFixed(1)}" fill="var(--apricot-500)" opacity="0.85"/>`;
    });
    html += '</svg>';
    div.innerHTML += html;
    return div;
  }
  function bucketize(raw, n) {
    if (!raw.length) return [];
    const min = Math.min(...raw), max = Math.max(...raw), step = (max - min) / n || 1;
    const bins = Array.from({length: n}, (_, i) => ({ x: min + i*step, count: 0 }));
    raw.forEach(v => {
      const idx = Math.min(n-1, Math.floor((v - min) / step));
      bins[idx].count++;
    });
    return bins;
  }
  function renderStatusGrid(b) {
    const div = el('div','block chart-block status-grid');
    if (b.label) div.appendChild(blockLabel(b.label));
    const cells = b.cells || [];
    const grid = el('div');
    grid.style.display = 'grid';
    grid.style.gridTemplateColumns = `repeat(${cells[0]?.length || 1}, 14px)`;
    grid.style.gap = '3px';
    cells.flat().forEach(s => {
      const c = el('div'); c.style.width = '14px'; c.style.height = '14px';
      c.style.borderRadius = '2px';
      c.style.background = STATUS_COLOR[s] || 'var(--neutral-300)';
      c.title = s;
      grid.appendChild(c);
    });
    div.appendChild(grid);
    return div;
  }
  function renderHeatmap(b) {
    const div = el('div','block chart-block heatmap');
    if (b.label) div.appendChild(blockLabel(b.label));
    const agents = b.agents || []; const buckets = b.buckets || []; const matrix = b.matrix || [];
    const max = Math.max(...matrix.flat(), 1);
    const wrap = el('div');
    wrap.style.display = 'grid';
    wrap.style.gridTemplateColumns = `120px repeat(${buckets.length}, 1fr)`;
    wrap.style.gap = '2px';
    wrap.style.fontFamily = 'var(--font-mono)';
    wrap.style.fontSize = '10px';
    // header
    wrap.appendChild(el('div',null,''));
    buckets.forEach(bk => { const h = el('div'); h.style.color = 'var(--mid)'; h.textContent = bk; wrap.appendChild(h); });
    agents.forEach((a, i) => {
      const lab = el('div'); lab.textContent = a; lab.style.color = 'var(--ink)'; wrap.appendChild(lab);
      (matrix[i] || []).forEach(v => {
        const c = el('div'); c.style.height = '14px'; c.style.borderRadius = '2px';
        const op = (v / max).toFixed(2);
        c.style.background = `color-mix(in oklch, var(--apricot-500) ${Math.round(op*100)}%, var(--bone))`;
        c.title = `${a} · ${v}`;
        wrap.appendChild(c);
      });
    });
    div.appendChild(wrap);
    return div;
  }
  function renderProgressArc(b) {
    const div = el('div','block chart-block progress-arc');
    if (b.label) div.appendChild(blockLabel(b.label));
    const v = b.value || 0, max = b.max || 100;
    const pct = Math.min(1, v / max);
    const r = 32, c = 2 * Math.PI * r;
    div.innerHTML += `
      <div style="display:flex;align-items:center;gap:14px;">
        <svg viewBox="0 0 80 80" style="width:80px;height:80px;">
          <circle cx="40" cy="40" r="${r}" fill="none" stroke="var(--bone)" stroke-width="6"/>
          <circle cx="40" cy="40" r="${r}" fill="none" stroke="var(--apricot-500)" stroke-width="6"
                  stroke-dasharray="${(c*pct).toFixed(1)} ${c.toFixed(1)}" stroke-linecap="round"
                  transform="rotate(-90 40 40)"/>
          <text x="40" y="44" text-anchor="middle" font-family="var(--font-mono)" font-size="14" fill="var(--ink)">${Math.round(pct*100)}%</text>
        </svg>
        <div><div style="font-family:var(--font-mono);font-size:12px;color:var(--ink);">${v} / ${max}</div>
        ${b.label ? `<div style="font-size:11px;color:var(--mid);">${escapeHtml(b.label)}</div>` : ''}</div>
      </div>`;
    return div;
  }
  function renderLaneFlow(b) {
    const div = el('div','block chart-block lane-flow');
    if (b.label) div.appendChild(blockLabel(b.label));
    const from = b.from || []; const to = b.to || []; const values = b.values || [];
    const total = values.reduce((a, v) => a + v, 0) || 1;
    const wrap = el('div');
    wrap.style.padding = '8px 0';
    from.forEach((f, i) => {
      const row = el('div');
      row.style.display = 'grid';
      row.style.gridTemplateColumns = '90px 1fr 90px';
      row.style.alignItems = 'center';
      row.style.gap = '8px';
      row.style.fontFamily = 'var(--font-mono)';
      row.style.fontSize = '11px';
      row.style.padding = '3px 0';
      const lf = el('div'); lf.textContent = f; lf.style.color = 'var(--mid)';
      const bar = el('div'); bar.style.height = '12px'; bar.style.background = 'var(--bone)'; bar.style.borderRadius = '2px'; bar.style.position = 'relative';
      const fill = el('div'); fill.style.height = '100%'; fill.style.background = 'var(--apricot-500)';
      fill.style.width = ((values[i] / total) * 100).toFixed(1) + '%';
      fill.style.borderRadius = '2px';
      bar.appendChild(fill);
      const lt = el('div'); lt.textContent = (to[i] || '') + ` · ${values[i]}`; lt.style.color = 'var(--ink)';
      row.appendChild(lf); row.appendChild(bar); row.appendChild(lt);
      wrap.appendChild(row);
    });
    div.appendChild(wrap);
    return div;
  }
  function renderDeltaBar(b) {
    const div = el('div','block chart-block delta-bar');
    const before = b.before || 0, after = b.after || 0;
    const delta = after - before;
    const dir = delta >= 0 ? '↑' : '↓';
    const col = delta >= 0 ? 'var(--success)' : 'var(--danger)';
    div.innerHTML = `
      <div style="display:flex;align-items:center;gap:14px;font-family:var(--font-mono);">
        <div style="display:flex;flex-direction:column;gap:2px;">
          <span style="color:var(--mid);font-size:10px;text-transform:uppercase;letter-spacing:0.06em;">before</span>
          <span style="font-size:18px;color:var(--ink);">${escapeHtml(String(before))}${b.unit ? ' '+escapeHtml(b.unit) : ''}</span>
        </div>
        <div style="font-size:24px;color:${col};">${dir}</div>
        <div style="display:flex;flex-direction:column;gap:2px;">
          <span style="color:var(--mid);font-size:10px;text-transform:uppercase;letter-spacing:0.06em;">after</span>
          <span style="font-size:18px;color:var(--ink);">${escapeHtml(String(after))}${b.unit ? ' '+escapeHtml(b.unit) : ''}</span>
        </div>
        <div style="margin-left:auto;color:${col};font-size:13px;">${dir} ${escapeHtml(String(Math.abs(delta)))} ${b.label ? escapeHtml(b.label) : ''}</div>
      </div>`;
    return div;
  }
  function renderChecklist(b) {
    const div = el('div','block checklist');
    if (b.label) div.appendChild(blockLabel(b.label, b.meta));
    const ul = el('ul');
    (b.items || []).forEach(it => {
      const li = el('li', it.done ? 'done' : null);
      const box = el('span','box', it.done ? '✓' : '');
      const lab = el('span'); lab.textContent = it.label || '';
      li.appendChild(box); li.appendChild(lab);
      ul.appendChild(li);
    });
    div.appendChild(ul);
    return div;
  }
  function renderDecision(b) {
    const div = el('div','block decision');
    const dlabel = el('div','dlabel');
    dlabel.innerHTML = `<span>decision</span><span class="verdict">${b.chosen ? 'CHOSEN' : 'OPEN'}</span>`;
    div.appendChild(dlabel);
    if (b.question) {
      const t = el('div','dtitle'); t.textContent = b.question; div.appendChild(t);
    }
    const opts = el('div','doptions');
    (b.options || []).forEach(opt => {
      const o = el('div', opt === b.chosen ? 'opt chosen' : 'opt');
      o.appendChild(el('div','opt-name', opt === b.chosen ? 'chosen' : 'option'));
      o.appendChild(el('div','opt-text', opt));
      opts.appendChild(o);
    });
    div.appendChild(opts);
    return div;
  }
  function renderLink(b) {
    const div = el('div','block link');
    const a = el('a'); a.href = b.href || '#'; a.target = '_blank'; a.rel = 'noopener';
    a.innerHTML = '<svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M5 3H3v6h6V7"/><path d="M7 2h3v3"/><path d="m6 6 4-4"/></svg>' + escapeHtml(b.label || b.href || '');
    div.appendChild(a);
    return div;
  }
  function renderKeyValue(b) {
    const dl = el('dl','block kv');
    if (b.label) {
      const lab = el('div','block-label');
      lab.style.gridColumn = '1/-1';
      lab.style.paddingLeft = '0';
      lab.style.borderLeft = 'none';
      lab.textContent = b.label;
      dl.appendChild(lab);
    }
    (b.rows || []).forEach(r => {
      const dt = el('dt'); dt.textContent = r.key;
      const dd = el('dd'); dd.textContent = r.value;
      dl.appendChild(dt); dl.appendChild(dd);
    });
    return dl;
  }

  // ─────────── Node renderer ───────────
  function renderNode(node) {
    const li = el('li','node');
    li.dataset.id = node.id;
    li.dataset.kind = node.kind || 'response';
    li.dataset.agent = node.agent || '';
    if (node.id) li.id = node.id;
    li.style.setProperty('--lane', laneVar(node.agent));

    const rail = el('div','rail');
    const marker = el('div','marker is-filled');
    if (node.status === 'in_progress') marker.classList.add('is-active');
    if (node.status === 'failed')   marker.style.setProperty('--lane', 'var(--danger)');
    if (node.status === 'decision') marker.style.setProperty('--lane', 'var(--apricot-600)');
    rail.appendChild(marker);
    li.appendChild(rail);

    const card = document.createElement('details');
    card.className = 'card';
    const status = node.status || 'completed';
    card.dataset.status = status === 'in_progress' ? 'in-progress' : status;
    if (status === 'in_progress') card.classList.add('is-active');
    if (status === 'in_progress' || status === 'decision') card.open = true;

    // ─── Summary (single compact row when closed) ───
    const sum = document.createElement('summary');
    sum.appendChild(el('span','lane-mark'));
    const h3 = el('h3','title'); h3.textContent = node.title || '(untitled)'; sum.appendChild(h3);

    const tag = el('span','status-tag');
    tag.appendChild(el('span','dot'));
    tag.appendChild(document.createTextNode(STATUS_LABEL[status] || status));
    sum.appendChild(tag);

    const tm = el('span','time');
    tm.innerHTML = `<time>${escapeHtml(shortTime(node.ts))}</time> <span class="rel">· ${escapeHtml(rel(node.ts))}</span>`;
    sum.appendChild(tm);

    const chev = el('span','chevron'); chev.innerHTML = chevronSvg();
    sum.appendChild(chev);
    card.appendChild(sum);

    // ─── Meta line (visible only when [open]) ───
    const meta = el('div','meta-line');
    const agent = el('span','agent-pill'); agent.style.setProperty('--lane', laneVar(node.agent));
    agent.appendChild(el('span','swatch'));
    agent.appendChild(document.createTextNode(' ' + (node.agent || '?')));
    if (node.session && node.session !== 'main') {
      const sid = el('span','id'); sid.textContent = node.session; agent.appendChild(sid);
    }
    meta.appendChild(agent);
    const st = el('span','status ' + (status === 'in_progress' ? 'in-progress' : status));
    if (status === 'in_progress') {
      st.appendChild(el('span','inline-dot'));
      st.appendChild(document.createTextNode(STATUS_LABEL[status]));
    } else {
      st.textContent = STATUS_LABEL[status] || status;
    }
    meta.appendChild(st);
    const isoT = el('span','iso-time');
    isoT.innerHTML = `<time>${escapeHtml(node.ts || '')}</time> <span class="rel">${escapeHtml(rel(node.ts))} ago</span>`;
    meta.appendChild(isoT);
    if (node.kind === 'fork' || node.kind === 'merge') {
      const k = el('span','status decision'); k.textContent = node.kind; meta.appendChild(k);
    }
    if (node.tags && node.tags.length) {
      const tagWrap = el('span');
      tagWrap.style.cssText = 'display:inline-flex;gap:4px;flex-wrap:wrap;margin-left:auto;';
      node.tags.forEach(t => {
        const p = el('span');
        p.style.cssText = 'font-family:var(--font-mono);font-size:10px;color:var(--mid);background:var(--bone);padding:1px 6px;border-radius:3px;border:1px solid var(--soft);';
        p.textContent = '#' + t;
        tagWrap.appendChild(p);
      });
      meta.appendChild(tagWrap);
    }
    card.appendChild(meta);

    // ─── Lede (visible only when [open]) ───
    if (node.summary) {
      const lede = el('div','titles-open');
      const p = el('p','lede'); p.textContent = node.summary;
      lede.appendChild(p);
      card.appendChild(lede);
    }

    // ─── Body (rich blocks, only when [open]) ───
    if (node.blocks && node.blocks.length) {
      const body = el('div','body');
      const blocks = el('div','blocks');
      node.blocks.forEach(b => {
        const r = renderers[b.type];
        if (r) blocks.appendChild(r(b));
        else blocks.appendChild(el('div','block', '⚠ unknown block type: ' + b.type));
      });
      body.appendChild(blocks);
      card.appendChild(body);
    }

    li.appendChild(card);
    return li;
  }

  // ─────────── Stats ribbon ───────────
  function renderStatsRibbon(nodes) {
    const root = document.getElementById('stats-ribbon');
    if (!root) return;
    if (!nodes.length) { root.style.display = 'none'; return; }
    const decisions = nodes.filter(n => n.status === 'decision' || n.kind === 'decision').length;
    const failures  = nodes.filter(n => n.status === 'failed').length;
    const agents    = new Set(nodes.map(n => n.agent)).size;
    const first = new Date(nodes[0].ts), last = new Date(nodes[nodes.length-1].ts);
    const elapsedMs = isNaN(first) || isNaN(last) ? 0 : (last - first);
    const elapsed = fmtElapsed(elapsedMs);
    const lastFail = nodes.filter(n => n.status === 'failed').slice(-1)[0];
    const throughput = elapsedMs > 0
      ? (nodes.length / (elapsedMs / 60000)).toFixed(2)
      : '—';
    root.innerHTML = `
      <div class="stat">
        <span class="label"><span class="swatch" style="background: var(--lane-trunk)"></span>nodes</span>
        <span class="v tabular">${nodes.length}</span>
        <span class="sub">over ${elapsed}</span>
      </div>
      <div class="stat">
        <span class="label"><span class="swatch" style="background: var(--apricot-600)"></span>decisions</span>
        <span class="v tabular">${decisions}</span>
        <span class="sub">${decisions ? 'last @ ' + shortTime(nodes.filter(n=>n.status==='decision').slice(-1)[0]?.ts) : 'none yet'}</span>
      </div>
      <div class="stat">
        <span class="label"><span class="swatch" style="background: var(--info)"></span>elapsed</span>
        <span class="v tabular">${elapsed}</span>
        <span class="sub">window ${shortTime(nodes[0].ts)} → ${shortTime(nodes[nodes.length-1].ts)}</span>
      </div>
      <div class="stat">
        <span class="label"><span class="swatch" style="background: var(--lane-graphite)"></span>agents</span>
        <span class="v tabular">${agents}</span>
        <span class="sub">${[...new Set(nodes.map(n=>n.agent))].join(' · ')}</span>
      </div>
      <div class="stat ${failures ? 'is-danger' : ''}">
        <span class="label"><span class="swatch" style="background: var(--danger)"></span>failures</span>
        <span class="v tabular">${failures}</span>
        <span class="sub">${lastFail ? 'last @ ' + shortTime(lastFail.ts) : 'none'}</span>
      </div>
      <div class="stat">
        <span class="label"><span class="swatch" style="background: var(--success)"></span>throughput</span>
        <span class="v tabular">${throughput}<span class="unit">n/m</span></span>
        <span class="sub">avg over window</span>
      </div>`;
    root.style.display = '';
  }

  // ─────────── Failure banner ───────────
  function renderFailureBanner(nodes) {
    const mount = document.getElementById('failure-banner-mount');
    const lastFail = nodes.filter(n => n.status === 'failed').slice(-1)[0];
    const app = document.getElementById('app');
    if (!lastFail) {
      mount.innerHTML = '';
      app.classList.remove('has-banner');
      document.getElementById('app-header').classList.remove('has-banner');
      return;
    }
    app.classList.add('has-banner');
    document.getElementById('app-header').classList.add('has-banner');
    mount.innerHTML = `
      <div class="failure-banner" role="alert">
        <span class="icon">
          <svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="7" cy="7" r="6"/><path d="M7 4v3.5M7 9.6v.4"/></svg>
        </span>
        <strong>Latest failure</strong>
        <span class="ts">at ${escapeHtml(shortTime(lastFail.ts))} UTC · ${escapeHtml(rel(lastFail.ts))} ago</span>
        <span style="opacity:0.7">— ${escapeHtml(lastFail.title || '')}</span>
        <button class="nav-link" onclick="document.getElementById('${escapeHtml(lastFail.id)}')?.scrollIntoView({behavior:'smooth',block:'center'})">jump ↓</button>
        <button class="dismiss" onclick="this.parentElement.remove(); document.getElementById('app').classList.remove('has-banner'); document.getElementById('app-header').classList.remove('has-banner');" aria-label="dismiss">
          <svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.4"><path d="m3 3 6 6m-6 0 6-6"/></svg>
        </button>
      </div>`;
  }

  // ─────────── Filters ───────────
  function buildFilters() {
    const nodes = window.TIMELINE_NODES || [];
    const statusBox  = document.getElementById('filter-status');
    const agentBox   = document.getElementById('filter-agent');
    const sessionBox = document.getElementById('filter-session');

    const statusCounts = { all: nodes.length };
    nodes.forEach(n => {
      const s = n.status || 'completed';
      statusCounts[s] = (statusCounts[s] || 0) + 1;
    });
    statusBox.innerHTML = '<span class="group-label">status</span>';
    Object.keys(statusCounts).forEach(s => {
      const chip = el('span','chip' + (s === activeStatus ? ' is-active' : ''));
      if (s !== 'all') {
        chip.style.color = STATUS_COLOR[s] || 'var(--mid)';
        chip.appendChild(el('span','dot'));
      }
      chip.appendChild(document.createTextNode(' ' + (STATUS_LABEL[s] || s) + ' '));
      chip.appendChild(el('span','count', statusCounts[s]));
      chip.addEventListener('click', () => { activeStatus = s; render(); });
      statusBox.appendChild(chip);
    });

    const agentCounts = { all: nodes.length };
    nodes.forEach(n => {
      const a = n.agent || 'unknown';
      agentCounts[a] = (agentCounts[a] || 0) + 1;
    });
    agentBox.innerHTML = '<span class="group-label">agent</span>';
    Object.keys(agentCounts).forEach(a => {
      const chip = el('span','chip' + (a === activeAgent ? ' is-active' : ''));
      if (a !== 'all') {
        chip.style.setProperty('--lane', laneVar(a));
        chip.style.color = laneVar(a);
        const sw = el('span','swatch');
        sw.style.cssText = 'background: currentColor;';
        chip.appendChild(sw);
      }
      chip.appendChild(document.createTextNode(' ' + a + ' '));
      chip.appendChild(el('span','count', agentCounts[a]));
      chip.addEventListener('click', () => { activeAgent = a; render(); });
      agentBox.appendChild(chip);
    });

    // Session filter chips — only render when there's more than one session.
    const sessionCounts = { all: nodes.length };
    nodes.forEach(n => {
      const s = n.session || 'main';
      sessionCounts[s] = (sessionCounts[s] || 0) + 1;
    });
    const distinctSessions = Object.keys(sessionCounts).filter(k => k !== 'all');
    if (sessionBox) {
      if (distinctSessions.length > 1) {
        sessionBox.style.display = '';
        sessionBox.innerHTML = '<span class="group-label">session</span>';
        Object.keys(sessionCounts).forEach(s => {
          const chip = el('span','chip' + (s === activeSession ? ' is-active' : ''));
          if (s !== 'all') chip.appendChild(el('span','dot'));
          chip.appendChild(document.createTextNode(' ' + s + ' '));
          chip.appendChild(el('span','count', sessionCounts[s]));
          chip.addEventListener('click', () => { activeSession = s; render(); });
          sessionBox.appendChild(chip);
        });
      } else {
        sessionBox.style.display = 'none';
        sessionBox.innerHTML = '';
      }
    }

    // Time filter chips
    const timeBox = document.getElementById('filter-time');
    if (timeBox) {
      timeBox.innerHTML = '<span class="group-label">time</span>';
      const timeRanges = [
        ['all',    'all'],
        ['1h',     'last 1h'],
        ['24h',    'last 24h'],
        ['7d',     'last 7d'],
        ['custom', 'custom'],
      ];
      timeRanges.forEach(([key, label]) => {
        const chip = el('span', 'chip' + (key === activeTimeRange ? ' is-active' : ''));
        chip.appendChild(document.createTextNode(label));
        chip.addEventListener('click', () => {
          activeTimeRange = key;
          if (key !== 'custom') { customFrom = ''; customTo = ''; }
          writeHash();
          render();
        });
        timeBox.appendChild(chip);
      });
    }

    // Custom date-range UI lives OUTSIDE the chip strip so it doesn't push
    // chips onto a new line when the user picks "custom".
    const customRow = document.getElementById('time-custom-row');
    if (customRow) {
      if (activeTimeRange === 'custom') {
        customRow.innerHTML = '';
        customRow.hidden = false;
        const lab = el('span','label','custom range');
        const fromI = document.createElement('input');
        fromI.type = 'datetime-local'; fromI.value = customFrom || '';
        fromI.addEventListener('change', () => { customFrom = fromI.value; writeHash(); render(); });
        const sep = el('span','sep','→');
        const toI = document.createElement('input');
        toI.type = 'datetime-local'; toI.value = customTo || '';
        toI.addEventListener('change', () => { customTo = toI.value; writeHash(); render(); });
        const clear = document.createElement('button');
        clear.type = 'button';
        clear.className = 'clear-btn';
        clear.textContent = 'clear';
        clear.addEventListener('click', () => {
          customFrom = ''; customTo = '';
          activeTimeRange = 'all';
          writeHash(); render();
        });
        customRow.appendChild(lab);
        customRow.appendChild(fromI);
        customRow.appendChild(sep);
        customRow.appendChild(toI);
        customRow.appendChild(clear);
      } else {
        customRow.hidden = true;
        customRow.innerHTML = '';
      }
    }
  }

  // v2.2: decision detection. A node is a "decision" if any of:
  //   - kind === 'decision' (explicit hook capture)
  //   - tags include 'decision'
  //   - status === 'decision' (legacy)
  //   - it's a prompt and the title contains a decision-verb
  const DECISION_VERBS = /\b(decide|chose|chosen|use(?!\sthe)|go with|pick|ship|skip|kill|drop|merge|deploy|approve|reject|defer)\b/i;
  function isDecision(n) {
    if (!n) return false;
    if (n.kind === 'decision' || n.status === 'decision') return true;
    if ((n.tags || []).indexOf('decision') !== -1) return true;
    if ((n.tags || []).indexOf('prompt') !== -1 && DECISION_VERBS.test(n.title || '')) return true;
    return false;
  }
  let decisionsOnly = false;

  function matches(n) {
    if (activeStatus !== 'all' && n.status !== activeStatus) return false;
    if (activeAgent  !== 'all' && n.agent  !== activeAgent)  return false;
    if (activeSession !== 'all' && (n.session || 'main') !== activeSession) return false;
    if (decisionsOnly && !isDecision(n)) return false;
    const bounds = timeBoundsForFilter();
    if (bounds && n.ts) {
      const t = new Date(n.ts).getTime();
      if (Number.isFinite(t)) {
        if (bounds.from != null && t < bounds.from) return false;
        if (bounds.to   != null && t > bounds.to)   return false;
      }
    }
    if (searchQuery) {
      const q = searchQuery.toLowerCase();
      const hay = ((n.title||'') + ' ' + (n.summary||'') + ' ' + (n.tags||[]).join(' ')).toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  }

  // ─────────── Multi-session render (v1.5: sidebar + detail pane) ───────────
  // Multi-column doesn't scale — three sessions and the screen is unreadable.
  // Instead: a left sidebar listing every session in this project (active /
  // stale / inactive), a single detail pane on the right showing the
  // selected session's full timeline. Same shape Linear, Slack, GitHub use
  // for N parallel things. Scales to 3 or 30 the same way.
  function classifySessionStatus(lastTs) {
    if (!lastTs) return 'inactive';
    const ms = Date.now() - new Date(lastTs).getTime();
    if (isNaN(ms)) return 'inactive';
    if (ms < 5 * 60 * 1000)        return 'active';
    if (ms < 24 * 60 * 60 * 1000)  return 'stale';
    return 'inactive';
  }

  function buildSessionMeta(nodes, sid) {
    const sNodes = nodes.filter(n => (n.session || 'main') === sid);
    const firstTs = sNodes[0]?.ts || '';
    const lastTs  = sNodes[sNodes.length - 1]?.ts || '';
    const turnsHere = new Set(sNodes.map(n => n.turn_id).filter(Boolean));
    const failures = sNodes.filter(n => n.status === 'failed').length;
    const firstPrompt = sNodes.find(n => isPromptNode(n));
    return {
      id: sid,
      nodes: sNodes,
      firstTs,
      lastTs,
      turns: turnsHere.size,
      events: sNodes.length,
      failures,
      status: classifySessionStatus(lastTs),
      firstPromptText: firstPrompt?.title || '',
    };
  }

  function renderMultiSession(root, nodes, sessionIds) {
    const sessions = sessionIds.map(sid => buildSessionMeta(nodes, sid));
    const statusOrder = { active: 0, stale: 1, inactive: 2 };
    sessions.sort((a, b) => {
      const so = statusOrder[a.status] - statusOrder[b.status];
      if (so !== 0) return so;
      return (b.lastTs || '').localeCompare(a.lastTs || '');
    });

    if (!activeSessionForView || !sessions.find(s => s.id === activeSessionForView)) {
      activeSessionForView = sessions[0]?.id || null;
    }

    const shell = el('div','viewer-shell is-multi');

    // v1.8: status-grouped sidebar with compact one-line rows.
    const sidebar = el('aside','session-sidebar v3-grouped');
    const head = el('div','head');
    const lab = el('span','label'); lab.textContent = 'sessions';
    const cnt = el('span','count'); cnt.textContent = sessions.length;
    head.appendChild(lab); head.appendChild(cnt);
    sidebar.appendChild(head);

    const groups = [
      { key: 'active',   label: 'active'   },
      { key: 'stale',    label: 'stale'    },
      { key: 'inactive', label: 'inactive' },
    ];
    groups.forEach(g => {
      const inGroup = sessions.filter(s => s.status === g.key);
      if (!inGroup.length) return;
      const block = el('div','group-block');
      const headBtn = document.createElement('button');
      headBtn.type = 'button';
      headBtn.className = 'group-head';
      headBtn.dataset.status = g.key;
      const isCollapsed = collapsedGroups.has(g.key);
      headBtn.setAttribute('aria-expanded', isCollapsed ? 'false' : 'true');
      headBtn.innerHTML =
        '<span class="swatch"></span>' +
        '<span class="label">' + escapeHtml(g.label) + '</span>' +
        '<span class="count">' + inGroup.length + '</span>' +
        '<svg class="chev" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.6">' +
        '<path d="M3 4l3 3 3-3"/></svg>';
      const body = el('div','group-body');
      body.setAttribute('aria-hidden', isCollapsed ? 'true' : 'false');
      headBtn.addEventListener('click', () => {
        if (collapsedGroups.has(g.key)) collapsedGroups.delete(g.key);
        else collapsedGroups.add(g.key);
        render();
      });
      inGroup.forEach(s => {
        const row = el('div','session-row compact');
        row.dataset.session = s.id;
        row.dataset.status  = s.status;
        if (s.id === activeSessionForView) row.classList.add('is-selected');
        row.appendChild(el('span','lane-stripe'));
        const idEl = el('span','id'); idEl.textContent = s.id;
        row.appendChild(idEl);
        const ageEl = el('span','age'); ageEl.textContent = rel(s.lastTs);
        row.appendChild(ageEl);
        row.title = `${s.turns || 0} turns · ${s.events} events` + (s.failures ? ` · ${s.failures} failed` : '');
        row.addEventListener('click', () => {
          activeSessionForView = s.id;
          writeHash();
          render();
        });
        body.appendChild(row);
      });
      block.appendChild(headBtn);
      block.appendChild(body);
      sidebar.appendChild(block);
    });

    shell.appendChild(sidebar);

    const pane = el('div','session-pane');
    const selected = sessions.find(s => s.id === activeSessionForView);
    if (!selected) {
      pane.appendChild(el('div','none','Select a session from the sidebar.'));
    } else {
      const ol = document.createElement('ol');
      ol.className = 'timeline';
      ol.style.padding = '0';
      ol.style.margin = '0';
      ol.style.listStyle = 'none';
      groupIntoTurns(selected.nodes).reverse().forEach(t => ol.appendChild(renderTurn(t)));
      pane.appendChild(ol);
    }
    shell.appendChild(pane);

    root.appendChild(shell);
  }

  // ─────────── Turn grouping (v1.1) ───────────
  // Each prompt opens a turn. Subsequent tool calls + sub-agent activity
  // belong to that turn. The Stop response closes it. Anything before any
  // prompt becomes an orphan turn (no header), so SessionStart and the like
  // still render visibly.
  function isPromptNode(n) {
    return (n.tags || []).indexOf('prompt') !== -1 || (n.agent === 'founder' && n.kind !== 'decision');
  }
  function isResponseNode(n) {
    return n.kind === 'response' && n.agent === 'orchestrator' && (n.tags || []).indexOf('response') !== -1;
  }
  function groupIntoTurns(nodes) {
    // v1.2: prefer node.turn_id (deterministic). For older nodes that
    // pre-date turn_id, fall back to the v1.1 prompt-boundary heuristic.
    const haveTurnIds = nodes.some(n => n && n.turn_id);
    if (haveTurnIds) return groupByTurnId(nodes);
    return groupByHeuristic(nodes);
  }

  function groupByTurnId(nodes) {
    const buckets = new Map();          // turn_id -> turn obj
    const orphans = { prompt: null, children: [], response: null, ts: null };
    nodes.forEach(n => {
      const tid = n.turn_id;
      if (!tid) {
        if (orphans.ts == null) orphans.ts = n.ts;
        orphans.children.push(n);
        return;
      }
      let t = buckets.get(tid);
      if (!t) {
        t = { id: tid, prompt: null, children: [], response: null, ts: n.ts };
        buckets.set(tid, t);
      }
      if (isPromptNode(n)) t.prompt = n;
      else if (isResponseNode(n)) t.response = n;
      else t.children.push(n);
      // Earliest ts wins as the turn ts (so we sort cleanly).
      if (!t.ts || (n.ts && n.ts < t.ts)) t.ts = n.ts;
    });
    const turns = [];
    if (orphans.children.length) turns.push(orphans);
    Array.from(buckets.values())
      .sort((a, b) => (a.ts || '').localeCompare(b.ts || ''))
      .forEach(t => turns.push(t));
    return turns;
  }

  function groupByHeuristic(nodes) {
    const turns = [];
    let current = null;
    nodes.forEach(n => {
      if (isPromptNode(n)) {
        if (current) turns.push(current);
        current = { prompt: n, children: [], response: null, ts: n.ts };
      } else if (current && isResponseNode(n)) {
        current.response = n;
        turns.push(current);
        current = null;
      } else if (current) {
        current.children.push(n);
      } else {
        turns.push({ prompt: null, children: [n], response: null, ts: n.ts });
      }
    });
    if (current) turns.push(current);
    return turns;
  }

  // v1.8: classify a child node by tool family (read/edit/bash/web/think/fail/other)
  function classifyChild(c) {
    if (c.status === 'failed') return 'fail';
    const tags = c.tags || [];
    if (tags.indexOf('read') !== -1)                                return 'read';
    if (tags.indexOf('wrote') !== -1 || tags.indexOf('edited') !== -1) return 'edit';
    if (tags.indexOf('bash') !== -1)                                return 'bash';
    if (tags.indexOf('search') !== -1 || tags.indexOf('web') !== -1) return 'web';
    if (tags.indexOf('fork') !== -1 || tags.indexOf('merge') !== -1) return 'think';
    return 'other';
  }
  const TOOL_LABELS = {
    read:  'read',
    edit:  'edit',
    bash:  'bash',
    web:   'web',
    think: 'subagent',
    fail:  'failed',
    other: 'other',
  };
  const TOOL_ORDER = ['read', 'edit', 'bash', 'web', 'think', 'fail', 'other'];

  function childTargetText(c) {
    // Pick the most useful one-line target string for the row.
    return (c.title || c.summary || '').trim();
  }

  // v2.2: shared child-row factory. Renders fork/merge tints, paired-id
  // badge, file-edit diff badge (+N −M), failure styling.
  function childRow(c) {
    const row = el('div','child-row');
    if (c.kind === 'fork')  row.classList.add('is-fork');
    if (c.kind === 'merge') row.classList.add('is-merge');

    const ts  = el('span','ts');     ts.textContent  = shortTime(c.ts);
    const tg  = el('span','target'); tg.textContent  = childTargetText(c);
    const me  = el('span','meta');
    if (c.status === 'failed') me.classList.add('fail');

    // File-edit diff badge — visible inline when the hook captured deltas.
    if (typeof c.lines_added === 'number' || typeof c.lines_removed === 'number') {
      const parts = [];
      if (c.lines_added)   parts.push('<span class="add">+' + c.lines_added + '</span>');
      if (c.lines_removed) parts.push('<span class="del">−' + c.lines_removed + '</span>');
      if (parts.length) {
        const diff = el('span','diff'); diff.innerHTML = parts.join(' ');
        diff.title = 'lines added / removed in this edit';
        row.appendChild(diff);
      }
    }

    // Paired dispatch id badge (fork↔merge match).
    if (c.source_tool_use_id) {
      const tuid = el('span','tuid');
      tuid.title = 'paired dispatch id: ' + c.source_tool_use_id;
      tuid.textContent = '↔ ' + c.source_tool_use_id.slice(-6);
      row.appendChild(tuid);
    }

    me.textContent = c.agent || '';
    row.appendChild(ts);
    row.appendChild(tg);
    row.appendChild(me);
    return row;
  }

  function turnSummary(turn) {
    // Build the "X reads · Y edits · Z bash · subagent: name" strip.
    const counts = {};
    let subagent = null;
    (turn.children || []).forEach(c => {
      const tags = c.tags || [];
      if (tags.indexOf('read') !== -1) counts.reads = (counts.reads || 0) + 1;
      else if (tags.indexOf('wrote') !== -1 || tags.indexOf('edited') !== -1) counts.edits = (counts.edits || 0) + 1;
      else if (tags.indexOf('bash') !== -1) counts.bash = (counts.bash || 0) + 1;
      else if (tags.indexOf('search') !== -1) counts.search = (counts.search || 0) + 1;
      else if (tags.indexOf('web') !== -1) counts.web = (counts.web || 0) + 1;
      else if (tags.indexOf('fork') !== -1 || tags.indexOf('merge') !== -1) {
        counts.subagent = (counts.subagent || 0) + 1;
        if (!subagent && c.agent && c.agent !== 'orchestrator' && c.agent !== 'founder') subagent = c.agent;
      } else {
        counts.other = (counts.other || 0) + 1;
      }
    });
    const parts = [];
    if (counts.reads)    parts.push(`<strong>${counts.reads}</strong> reads`);
    if (counts.edits)    parts.push(`<strong>${counts.edits}</strong> edits`);
    if (counts.bash)     parts.push(`<strong>${counts.bash}</strong> bash`);
    if (counts.search)   parts.push(`<strong>${counts.search}</strong> search`);
    if (counts.web)      parts.push(`<strong>${counts.web}</strong> web`);
    if (counts.subagent) parts.push(`<strong>${counts.subagent}</strong> sub-agent`);
    if (subagent) parts.push(`→ <em>${escapeHtml(subagent)}</em>`);
    if (counts.other && !parts.length) parts.push(`<strong>${counts.other}</strong> tool calls`);
    return parts.join(' · ');
  }

  // ─── v2.0: SVG helpers — innerHTML on <svg> drops SVG-namespaced
  // children in some browsers. createElementNS for everything.
  const SVG_NS = 'http://www.w3.org/2000/svg';
  function svgEl(name, attrs) {
    const e = document.createElementNS(SVG_NS, name);
    if (attrs) for (const k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  }
  // Build N curves fanning out from trunk (top center) to N lane heads
  // (bottom, evenly spaced), or N curves converging back to trunk.
  // Each curve is colored by its lane.
  function buildFanSvg(direction, laneColors) {
    const n = laneColors.length;
    const W = 1200, H = 56;
    const svg = svgEl('svg', {
      'class': 'dispatch-curve dispatch-curve-' + direction,
      viewBox: '0 0 ' + W + ' ' + H,
      preserveAspectRatio: 'none',
    });
    // Trunk x is at left (close to dispatch-card's left edge).
    const trunkX = 28;
    for (let i = 0; i < n; i++) {
      // Lane center x along the bottom: spread evenly across width
      const laneX = n === 1
        ? W * 0.5
        : 28 + (W - 56) * (i + 0.5) / n;
      // Fork: curve from (trunkX, 0) down to (laneX, H)
      // Merge: curve from (laneX, 0) down to (trunkX, H)
      const startX = direction === 'fork' ? trunkX : laneX;
      const endX   = direction === 'fork' ? laneX   : trunkX;
      const d = `M${startX} 0 C${startX} ${H * 0.7}, ${endX} ${H * 0.3}, ${endX} ${H}`;
      svg.appendChild(svgEl('path', {
        d,
        stroke: laneColors[i],
        'stroke-width': '2',
        'stroke-linecap': 'round',
        fill: 'none',
        opacity: '0.9',
      }));
    }
    return svg;
  }

  // ─── v2.0: Dispatch-group rendering ───────────────────────────────
  // Detect paired fork+merge nodes by source_tool_use_id within a turn's
  // children. Adjacent-in-time forks (within CLUSTER_WINDOW_MS) get grouped
  // into ONE parallel-dispatch unit with N lanes. Each lane has:
  //   { fork, merges[], merge, subagentName, tuid }
  // Returns: array of cluster objects: { lanes[], firstFork, lastMergeOrFork }
  const CLUSTER_WINDOW_MS = 30 * 1000;       // forks within 30s = same parallel dispatch
  function collectDispatchGroups(children) {
    if (!children || !children.length) return [];
    const forks = children
      .filter(c => c.kind === 'fork' && c.source_tool_use_id)
      .slice()
      .sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts));
    if (!forks.length) return [];

    function laneFor(f) {
      const merges = children.filter(c =>
        c.kind === 'merge' && c.source_tool_use_id === f.source_tool_use_id
      );
      const subagentName = merges.length && merges[0].agent
        ? merges[0].agent
        : (f.title || '').match(/Dispatched → ([^:]+)/)?.[1]?.trim() || 'subagent';
      return {
        fork: f,
        merges,
        merge: merges[0] || null,
        subagentName,
        tuid: f.source_tool_use_id,
      };
    }

    // Cluster adjacent-in-time forks. New cluster when gap > CLUSTER_WINDOW_MS.
    const clusters = [];
    for (const f of forks) {
      const lane = laneFor(f);
      const last = clusters[clusters.length - 1];
      if (!last) { clusters.push({ lanes: [lane] }); continue; }
      const prevTs = Date.parse(last.lanes[last.lanes.length - 1].fork.ts);
      const thisTs = Date.parse(f.ts);
      if (Number.isFinite(prevTs) && Number.isFinite(thisTs) && (thisTs - prevTs) <= CLUSTER_WINDOW_MS) {
        last.lanes.push(lane);
      } else {
        clusters.push({ lanes: [lane] });
      }
    }
    // Convenience pointers for rendering
    for (const c of clusters) {
      c.firstFork = c.lanes[0].fork;
      // Latest merge across the cluster (for the merge-card timestamp)
      const allMerges = c.lanes.flatMap(l => l.merges);
      c.latestMerge = allMerges.length
        ? allMerges.reduce((a, b) => Date.parse(a.ts) > Date.parse(b.ts) ? a : b)
        : null;
    }
    return clusters;
  }

  // Lane color for a subagent name — uses the same palette as the trunk's
  // AGENT_LANE table where possible, otherwise picks deterministically.
  function laneColorFor(subagent) {
    const known = AGENT_LANE[subagent];
    if (known) return known;
    const palette = ['var(--info)', 'var(--success)', 'var(--apricot-600)', 'var(--warning)'];
    let h = 0; for (const c of subagent) h = (h * 31 + c.charCodeAt(0)) >>> 0;
    return palette[h % palette.length];
  }

  // Render a parallel dispatch cluster (1 → N → 1) as the iter6 visual.
  //   ┌─────────────────────────────────────────────────────────┐
  //   │  ▸ FORK dispatch-card                                   │
  //   ├─────────────────────────────────────────────────────────┤
  //   │  ╲   |   ╱      fork SVG: N lane-colored curves         │
  //   ├─────┬─────┬─────┤
  //   │ lbl │ lbl │ lbl │  lane labels (N columns)
  //   │ body│ body│ body│  lane bodies (wall-time / status)
  //   ├─────┴─────┴─────┤
  //   │  ╱   |   ╲      merge SVG: N curves converging back     │
  //   ├─────────────────────────────────────────────────────────┤
  //   │  ▸ MERGE merge-card                                     │
  //   │    │ col │ col │ col │  contribution columns (N)        │
  //   └─────────────────────────────────────────────────────────┘
  function renderDispatchGroup(cluster) {
    const lanes = cluster.lanes;
    const n = lanes.length;
    const laneColors = lanes.map(l => laneColorFor(l.subagentName));

    const wrap = el('div','dispatch-group');
    wrap.dataset.lanes = String(n);
    wrap.dataset.tuid = lanes[0].tuid || '';
    wrap.style.setProperty('--lane-count', String(n));
    wrap.style.setProperty('--lane', laneColors[0]);

    // ── 1. Dispatch (fork) card ─────────────────────────────────────
    const forkCard = el('div','dispatch-card');
    const fhead = el('div','dispatch-head');
    const firstFork = cluster.firstFork;
    const title = n === 1
      ? (firstFork.title || 'Dispatched → ' + lanes[0].subagentName)
      : 'Dispatch — ' + n + ' sub-agents in parallel';
    fhead.innerHTML =
      '<span class="dispatch-tag">FORK</span>' +
      '<span class="dispatch-title">' + escapeHtml(title) + '</span>' +
      (n === 1
        ? '<span class="dispatch-tuid" title="' + escapeHtml(lanes[0].tuid || '') + '">↔ ' +
            escapeHtml((lanes[0].tuid || '').slice(-6)) + '</span>'
        : '<span class="dispatch-tuid">' + n + ' lanes</span>') +
      '<span class="dispatch-time">' + escapeHtml(shortTime(firstFork.ts)) + '</span>';
    forkCard.appendChild(fhead);
    if (n === 1 && firstFork.summary) {
      const s = el('p','dispatch-summary'); s.textContent = firstFork.summary;
      forkCard.appendChild(s);
    } else if (n > 1) {
      const s = el('p','dispatch-summary');
      s.textContent = lanes.map(l => l.subagentName).join(' · ');
      forkCard.appendChild(s);
    }
    wrap.appendChild(forkCard);

    // ── 2. Fork SVG fan-out ─────────────────────────────────────────
    wrap.appendChild(buildFanSvg('fork', laneColors));

    // ── 3. Lane headers + bodies in CSS grid ────────────────────────
    const laneRow = el('div','dispatch-lanes');
    laneRow.style.setProperty('--lane-count', String(n));
    lanes.forEach((lane, idx) => {
      const col = el('div','dispatch-lane-col');
      col.style.setProperty('--lane', laneColors[idx]);
      // Lane label
      const lbl = el('div','dispatch-lane-label');
      lbl.innerHTML =
        '<span class="swatch"></span>' +
        '<span class="name">' + escapeHtml(lane.subagentName) + '</span>' +
        '<span class="id">' + escapeHtml((lane.tuid || '').slice(-4)) + '</span>';
      col.appendChild(lbl);
      // Lane body
      const body = el('div','dispatch-lane-body');
      if (lane.merge) {
        const ms = Date.parse(lane.merge.ts) - Date.parse(lane.fork.ts);
        const elapsed = Number.isFinite(ms) && ms > 0 ? fmtElapsed(ms) : '—';
        body.innerHTML = '<span class="meta">ran <strong>' + escapeHtml(elapsed) + '</strong></span>';
      } else {
        body.innerHTML = '<span class="meta dim">in flight…</span>';
      }
      col.appendChild(body);
      laneRow.appendChild(col);
    });
    wrap.appendChild(laneRow);

    // ── 4. Merge SVG fan-in (only if at least one lane has a merge) ──
    const anyMerge = lanes.some(l => l.merge);
    if (anyMerge) {
      wrap.appendChild(buildFanSvg('merge', laneColors));
    }

    // ── 5. Merge card with N contribution columns ───────────────────
    if (anyMerge) {
      const mergeCard = el('div','dispatch-merge-card');
      const mhead = el('div','dispatch-merge-head');
      const mergeTitle = n === 1
        ? (lanes[0].merge.title || (lanes[0].subagentName + ' done'))
        : 'All sub-agents converged — ' + n + ' lanes';
      mhead.innerHTML =
        '<span class="merge-tag">MERGE · ' + n + ' → 1</span>' +
        '<span class="merge-title">' + escapeHtml(mergeTitle) + '</span>' +
        '<span class="merge-time">' + escapeHtml(shortTime((cluster.latestMerge || firstFork).ts)) + '</span>' +
        '<span class="agents-summary">' +
          lanes.map((_, i) =>
            '<span class="agent-swatch" style="background:' + laneColors[i] + '"></span>'
          ).join('') +
        '</span>';
      mergeCard.appendChild(mhead);

      const contribs = el('div','dispatch-contribs');
      contribs.style.setProperty('--lane-count', String(n));
      lanes.forEach((lane, idx) => {
        const col = el('div','dispatch-contrib');
        col.style.setProperty('--lane', laneColors[idx]);
        const head = el('div','contrib-head');
        head.innerHTML =
          '<span class="swatch"></span>' +
          '<span class="name">' + escapeHtml(lane.subagentName) + '</span>' +
          '<span class="id">· ' + escapeHtml((lane.tuid || '').slice(-4)) + '</span>';
        col.appendChild(head);
        if (lane.merge && lane.merge.summary) {
          const p = el('p','contrib-body'); p.textContent = lane.merge.summary;
          col.appendChild(p);
        } else if (!lane.merge) {
          const p = el('p','contrib-body dim'); p.textContent = 'in flight…';
          col.appendChild(p);
        }
        contribs.appendChild(col);
      });
      mergeCard.appendChild(contribs);
      wrap.appendChild(mergeCard);
    }

    return wrap;
  }

  function renderTurn(turn) {
    const li = el('li','turn');
    if (turn.prompt) {
      li.dataset.id = turn.prompt.id;
      li.id = turn.prompt.id;
    }
    // Rail with the trunk dot anchored on the prompt.
    const rail = el('div','rail');
    const marker = el('div','marker is-filled');
    if (turn.prompt && turn.prompt.agent === 'founder') {
      marker.style.setProperty('--lane', 'var(--apricot-600)');
    }
    rail.appendChild(marker);
    li.appendChild(rail);

    // Outer card representing the turn.
    const card = document.createElement('details');
    card.className = 'card turn-card';
    card.dataset.status = (turn.prompt ? turn.prompt.status : (turn.children[0] && turn.children[0].status) || 'completed');
    // Default open if it's the latest turn (no response = in progress).
    if (!turn.response) card.open = true;
    // v2.0: also auto-open turns that contain paired fork+merge dispatch
    // groups, so the iter6 dispatch-group visual is immediately visible.
    const turnDispatchClusters = collectDispatchGroups(turn.children || []);
    const turnLaneCount = turnDispatchClusters.reduce((s, c) => s + c.lanes.length, 0);
    if (turnDispatchClusters.length) {
      card.open = true;
      card.classList.add('has-dispatch');
      li.dataset.dispatchClusters = turnDispatchClusters.length;
      li.dataset.dispatchLanes = turnLaneCount;
    }

    const sum = document.createElement('summary');
    sum.appendChild(el('span','lane-mark'));

    if (turn.prompt) {
      const pillEl = el('span','agent-pill');
      pillEl.style.setProperty('--lane', laneVar('founder'));
      pillEl.appendChild(el('span','swatch'));
      pillEl.appendChild(document.createTextNode(' you'));
      sum.appendChild(pillEl);

      const titleEl = el('h3','title');
      titleEl.textContent = turn.prompt.title || '(empty prompt)';
      sum.appendChild(titleEl);
    } else {
      const titleEl = el('h3','title');
      titleEl.textContent = '(session events)';
      titleEl.style.color = 'var(--mid)';
      titleEl.style.fontStyle = 'italic';
      sum.appendChild(titleEl);
    }

    const tag = el('span','status-tag');
    tag.appendChild(el('span','dot'));
    tag.appendChild(document.createTextNode(turn.response ? 'replied' : (turn.children.length ? 'working…' : 'open')));
    sum.appendChild(tag);

    const tm = el('span','time');
    tm.innerHTML = `<time>${escapeHtml(shortTime(turn.ts))}</time> <span class="rel">· ${escapeHtml(rel(turn.ts))}</span>`;
    sum.appendChild(tm);

    const chev = el('span','chevron');
    chev.innerHTML = chevronSvg();
    sum.appendChild(chev);
    card.appendChild(sum);

    // Activity strip — shown ALWAYS so collapsed cards still tell the story.
    if (turn.children.length || turn.response) {
      const strip = el('div','turn-strip');
      const activity = turnSummary(turn);
      const respPreview = turn.response ? `<span class="resp-preview">↪ ${escapeHtml(turn.response.summary || turn.response.title || '')}</span>` : '';
      // v2.0: dispatch-group count badge if this turn contains any.
      let dispatchBadge = '';
      if (turnDispatchClusters.length) {
        const names = [...new Set(turnDispatchClusters.flatMap(c => c.lanes.map(l => l.subagentName)))].slice(0, 3).join(', ');
        const noun = turnLaneCount === 1 ? 'dispatch' : 'dispatches';
        dispatchBadge =
          '<span class="dispatch-badge" title="paired fork+merge dispatch groups">' +
          '🔀 ' + turnLaneCount + ' subagent ' + noun +
          (names ? ' · ' + escapeHtml(names) : '') +
          '</span>';
      }
      strip.innerHTML = dispatchBadge + (dispatchBadge && (activity || respPreview) ? ' · ' : '') +
                       (activity || '') + (activity && respPreview ? ' · ' : '') + respPreview;
      sum.parentNode.insertBefore(strip, sum.nextSibling);
    }

    // Body — only when expanded.
    // v1.8: `constrained` caps prose blocks at --prose-width (72ch) for
    // readability; tables / code / charts still go full-bleed.
    const body = el('div','body turn-body constrained');

    // 1. Full prompt body
    if (turn.prompt && turn.prompt.blocks && turn.prompt.blocks.length) {
      const promptSection = el('div','turn-section');
      promptSection.appendChild(el('div','section-label','prompt'));
      const inner = el('div','blocks');
      turn.prompt.blocks.forEach(b => {
        const r = renderers[b.type];
        if (r) inner.appendChild(r(b));
      });
      promptSection.appendChild(inner);
      body.appendChild(promptSection);
    }

    // 2. v1.8 — children grouped by tool family (read/edit/bash/web/think/fail).
    //    Each group is collapsed by default; click expands. First 5 rows visible,
    //    "show all N" reveals the rest.
    if (turn.children.length) {
      const turnKey = (turn.id || turn.prompt?.id || (turn.children[0] && turn.children[0].id) || 'orphan');
      const childSection = el('div','turn-section');
      childSection.appendChild(el('div','section-label', `during this turn · ${turn.children.length}`));

      // v2.0: detect paired fork+merge by source_tool_use_id and cluster
      // adjacent forks into one parallel-dispatch block (iter6 1→N→1 visual).
      const dispatchClusters = collectDispatchGroups(turn.children);
      const usedInDispatch = new Set();
      for (const c of dispatchClusters) {
        for (const lane of c.lanes) {
          if (lane.fork)  usedInDispatch.add(lane.fork.id);
          for (const m of lane.merges) usedInDispatch.add(m.id);
        }
      }
      if (dispatchClusters.length) {
        const dispatchSection = el('div','dispatch-groups');
        dispatchClusters.forEach(c => dispatchSection.appendChild(renderDispatchGroup(c)));
        childSection.appendChild(dispatchSection);
      }

      // Bucket remaining children by tool family.
      const buckets = new Map();
      turn.children.forEach(c => {
        if (usedInDispatch.has(c.id)) return;
        const tool = classifyChild(c);
        if (!buckets.has(tool)) buckets.set(tool, []);
        buckets.get(tool).push(c);
      });

      const groupsWrap = el('div','child-groups');
      TOOL_ORDER.forEach(tool => {
        const items = buckets.get(tool);
        if (!items || !items.length) return;
        const groupKey = `${turnKey}:${tool}`;
        const isExpanded = expandedChildGroups.has(groupKey);
        const showAll    = expandedShowAll.has(groupKey);
        const visible = showAll ? items : items.slice(0, 5);
        const lastTs = items[items.length - 1]?.ts;

        const group = el('div','child-group');
        group.dataset.tool = tool;
        group.setAttribute('aria-expanded', isExpanded ? 'true' : 'false');

        const head = document.createElement('button');
        head.type = 'button';
        head.className = 'child-group-head';
        head.innerHTML =
          '<svg class="chev" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M3 4l3 3 3-3"/></svg>' +
          `<span class="tool-tag"><span class="swatch"></span>${escapeHtml(TOOL_LABELS[tool] || tool)}</span>` +
          `<span class="count">${items.length}</span>` +
          `<span class="summary">last <strong>${escapeHtml(shortTime(lastTs))}</strong></span>`;
        // Collapse / expand the child-group locally — DO NOT call render().
        // A full re-render rebuilds every <details> from scratch and loses
        // the parent turn-card's open state. CSS reacts to aria-expanded so
        // a local toggle is sufficient. stopPropagation prevents the click
        // bubbling up to the parent <details>.
        head.addEventListener('click', (e) => {
          e.preventDefault();
          e.stopPropagation();
          const wasExpanded = group.getAttribute('aria-expanded') === 'true';
          if (wasExpanded) {
            group.setAttribute('aria-expanded', 'false');
            expandedChildGroups.delete(groupKey);
          } else {
            group.setAttribute('aria-expanded', 'true');
            expandedChildGroups.add(groupKey);
          }
        });
        group.appendChild(head);

        const groupBody = el('div','child-group-body');
        visible.forEach(c => {
          const row = childRow(c);
          groupBody.appendChild(row);
        });
        if (items.length > 5 && !showAll) {
          const more = document.createElement('button');
          more.type = 'button';
          more.className = 'more-link';
          more.textContent = `show all ${items.length} ${TOOL_LABELS[tool] || tool} →`;
          // Show-all also stays local — replace just this body's rows in-place.
          more.addEventListener('click', (e) => {
            e.preventDefault();
            e.stopPropagation();
            expandedShowAll.add(groupKey);
            expandedChildGroups.add(groupKey);
            const newBody = el('div','child-group-body');
            items.forEach(c => { newBody.appendChild(childRow(c)); });
            groupBody.replaceWith(newBody);
          });
          groupBody.appendChild(more);
        }
        group.appendChild(groupBody);
        groupsWrap.appendChild(group);
      });
      childSection.appendChild(groupsWrap);
      body.appendChild(childSection);
    }

    // 3. Claude's response
    if (turn.response) {
      const respSection = el('div','turn-section turn-response');
      respSection.appendChild(el('div','section-label','response'));
      const inner = el('div','blocks');
      if (turn.response.blocks && turn.response.blocks.length) {
        turn.response.blocks.forEach(b => {
          const r = renderers[b.type];
          if (r) inner.appendChild(r(b));
        });
      } else {
        const md = el('div','block md');
        md.textContent = turn.response.title || '';
        inner.appendChild(md);
      }
      respSection.appendChild(inner);
      body.appendChild(respSection);
    }

    card.appendChild(body);
    li.appendChild(card);
    return li;
  }

  // ─────────── Render ───────────
  function render() {
    const allNodes = (window.TIMELINE_NODES || []).slice().sort((a, b) => {
      if (a.ts === b.ts) return (a.id||'').localeCompare(b.id||'');
      return (a.ts||'').localeCompare(b.ts||'');
    });
    const visible = allNodes.filter(matches);
    const root = document.getElementById('timeline');
    root.innerHTML = '';
    if (!visible.length) {
      root.appendChild(el('div','empty','No nodes match the current filters.'));
    } else if (activeMode === 'events') {
      // Raw event log: each node as its own card (v1.0 behavior).
      visible.forEach(n => root.appendChild(renderNode(n)));
    } else {
      // v1.3: detect parallel sessions in the same project. If >1 distinct
      // session id is present (and the user hasn't filtered to a single one),
      // render as side-by-side columns. Otherwise single-column turn cards.
      const sessionIds = Array.from(new Set(visible.map(n => n.session || 'main')));
      if (sessionIds.length > 1 && activeSession === 'all') {
        renderMultiSession(root, visible, sessionIds);
      } else {
        // Latest turn on top — descending chronological order.
        const turns = groupIntoTurns(visible).reverse();
        turns.forEach(t => root.appendChild(renderTurn(t)));
      }
    }

    // header counts
    document.getElementById('meta-count').textContent = visible.length;
    // v2.2: decisions count on the pin button
    const decBtnCount = document.getElementById('btn-decisions-count');
    if (decBtnCount) decBtnCount.textContent = allNodes.filter(isDecision).length;
    if (allNodes.length) {
      const first = new Date(allNodes[0].ts), last = new Date(allNodes[allNodes.length-1].ts);
      const ms = isNaN(first) || isNaN(last) ? 0 : (last - first);
      document.getElementById('meta-elapsed').textContent = fmtElapsed(ms);
      document.getElementById('meta-window').innerHTML =
        `window <strong>${shortTime(allNodes[0].ts)} → ${shortTime(allNodes[allNodes.length-1].ts)}</strong>`;
    }

    renderStatsRibbon(allNodes);
    renderFailureBanner(allNodes);
    buildFilters();
    applyViewMode();
  }

  function applyViewMode() {
    const app = document.getElementById('app');
    app.classList.remove('is-summary-mode-on','is-summary-mode-off','is-compact-mode');
    if (activeMode === 'summary') {
      app.classList.add('is-summary-mode-on');
    } else if (activeMode === 'compact') {
      app.classList.add('is-compact-mode');
      document.querySelectorAll('details.card').forEach(d => d.open = false);
    } else {
      app.classList.add('is-summary-mode-off');
    }
  }

  // ─────────── Auto-refresh (poll nodes.js) ───────────
  // v1.0: re-fetch nodes.js every REFRESH_MS; rerender if it changed.
  // No SSE — keeps the server zero-magic.
  const REFRESH_MS = 5000;
  let lastNodesText = '';

  async function pollOnce() {
    try {
      const r = await fetch('data/nodes.js?_t=' + Date.now(), { cache: 'no-store' });
      if (!r.ok) return;
      const txt = await r.text();
      if (txt === lastNodesText) return;
      lastNodesText = txt;
      const m = txt.match(/window\.TIMELINE_NODES\s*=\s*(\[[\s\S]*\])\s*;/);
      if (!m) return;
      try {
        window.TIMELINE_NODES = JSON.parse(m[1]);
      } catch (_) { return; }
      // Preserve which cards are open across rerenders.
      const openIds = new Set();
      document.querySelectorAll('details.card[open]').forEach(d => {
        const li = d.closest('li.node');
        if (li && li.dataset.id) openIds.add(li.dataset.id);
      });
      render();
      openIds.forEach(id => {
        try {
          const li = document.querySelector(`li.node[data-id="${CSS.escape(id)}"]`);
          if (li) {
            const card = li.querySelector('details.card');
            if (card) card.open = true;
          }
        } catch (_) { /* CSS.escape may not exist on very old browsers */ }
      });
    } catch (_) { /* fail-soft */ }
  }

  // ─────────── URL hash routing ───────────
  // Format: #key=val&key=val (URL-encoded). Backward-compat: also accept the
  // older #session/<id> path form.
  function readHash() {
    const raw = (location.hash || '').replace(/^#/, '');
    if (!raw) return;
    const legacy = raw.match(/^session\/(.+)$/);
    if (legacy) {
      try { activeSessionForView = decodeURIComponent(legacy[1]); }
      catch (_) { activeSessionForView = legacy[1]; }
      return;
    }
    let params;
    try { params = new URLSearchParams(raw); }
    catch (_) { return; }
    if (params.has('session')) activeSessionForView = params.get('session') || null;
    if (params.has('since')) {
      const v = params.get('since');
      if (['all', '1h', '24h', '7d', 'custom'].indexOf(v) !== -1) activeTimeRange = v;
    }
    if (params.has('from')) customFrom = params.get('from') || '';
    if (params.has('to'))   customTo   = params.get('to')   || '';
  }
  function writeHash() {
    const params = new URLSearchParams();
    if (activeSessionForView)        params.set('session', activeSessionForView);
    if (activeTimeRange !== 'all')   params.set('since', activeTimeRange);
    if (activeTimeRange === 'custom') {
      if (customFrom) params.set('from', customFrom);
      if (customTo)   params.set('to',   customTo);
    }
    const s = params.toString();
    try {
      const target = s ? '#' + s : location.pathname + location.search;
      history.replaceState(null, '', target);
    } catch (_) {}
  }

  // ─────────── Boot ───────────
  // Set the page title to the project name (like Claude Code's terminal does).
  // Two paths: /c/<slug>/ (served via hub) → use slug; otherwise fetch from
  // /api/project-name on the per-project server.
  function setProjectTitle(name) {
    const h1 = document.getElementById('project-title');
    if (!h1 || !name) return;
    h1.innerHTML = escapeHtml(name) + '<span class="qualifier"> — live timeline</span>';
    document.title = name + ' · ClaudeCadence';
  }
  function wireProjectTitle() {
    const m = location.pathname.match(/^\/c\/([^/]+)/);
    if (m) {
      let slug;
      try { slug = decodeURIComponent(m[1]); } catch (_) { slug = m[1]; }
      setProjectTitle(slug);
      return;
    }
    fetch('api/project-name', { cache: 'no-store' })
      .then(r => r.ok ? r.json() : null)
      .then(j => { if (j && j.name) setProjectTitle(j.name); })
      .catch(() => {});
  }

  // v2.2: show the running plugin version in the chrome. Source of truth is
  // /api/version served by cadence-serve; quietly hide on miss.
  function wireVersionTag() {
    const tag = document.getElementById('version-tag');
    if (!tag) return;
    fetch('api/version', { cache: 'no-store' })
      .then(r => r.ok ? r.json() : null)
      .then(j => {
        if (j && j.version) {
          tag.textContent = 'v' + j.version;
          tag.href = 'https://github.com/RohitSh26/ClaudeCadence/releases/tag/v' + j.version;
        } else {
          tag.style.display = 'none';
        }
      })
      .catch(() => { tag.style.display = 'none'; });
  }

  // Resolve the hub URL — three cases:
  //   1. We're served under /c/<slug>/ (via hub) → link to "/"
  //   2. We're served per-project, hub is running → fetch /api/hub-url, link to it
  //   3. Hub not running → keep the link hidden
  function wireHomeLink() {
    const link = document.getElementById('home-link');
    if (!link) return;
    if (location.pathname.startsWith('/c/')) {
      link.href = '/';
      link.hidden = false;
      return;
    }
    fetch('api/hub-url', { cache: 'no-store' })
      .then(r => r.ok ? r.json() : null)
      .then(j => {
        if (j && j.url) {
          link.href = j.url;
          link.hidden = false;
        }
      })
      .catch(() => {});
  }

  document.addEventListener('DOMContentLoaded', () => {
    readHash();
    window.addEventListener('hashchange', () => { readHash(); render(); });
    wireHomeLink();
    wireProjectTitle();
    wireVersionTag();
    render();
    // Seed polling baseline so we don't immediately rerender on first tick.
    fetch('data/nodes.js?_t=' + Date.now(), { cache: 'no-store' })
      .then(r => r.ok ? r.text() : '')
      .then(t => { lastNodesText = t; })
      .catch(() => {});
    setInterval(pollOnce, REFRESH_MS);

    document.getElementById('search').addEventListener('input', e => {
      searchQuery = e.target.value; render();
    });
    document.getElementById('btn-collapse-all').addEventListener('click', () => {
      document.querySelectorAll('details.card').forEach(d => d.open = false);
    });
    document.getElementById('btn-expand-all').addEventListener('click', () => {
      document.querySelectorAll('details.card').forEach(d => d.open = true);
    });
    // v2.2 — Decisions filter pin
    const decBtn = document.getElementById('btn-decisions');
    if (decBtn) {
      decBtn.addEventListener('click', () => {
        decisionsOnly = !decisionsOnly;
        decBtn.classList.toggle('is-active', decisionsOnly);
        decBtn.setAttribute('aria-pressed', String(decisionsOnly));
        render();
      });
    }
    document.getElementById('view-mode').addEventListener('click', e => {
      const btn = e.target.closest('button[data-mode]');
      if (!btn) return;
      activeMode = btn.dataset.mode;
      document.querySelectorAll('#view-mode button').forEach(b => b.classList.toggle('is-active', b === btn));
      applyViewMode();
    });
    document.addEventListener('keydown', e => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault(); document.getElementById('search').focus();
      }
    });
  });
})();
