// Схема архитектуры ядра на Cytoscape.js (как графы GUI): холст с панорамой и масштабом, блоки перетаскиваются,
// по клику блок разворачивается на месте, справа описание. Данные: window.ARCH_DATA (architecture-data.js).
// Используется во вкладке «Схема» GUI и на странице docs/architecture-clean.html.
(function () {
  const NW = 196, NH = 50, GAP = 10, PAD = 24, TOP_PAD = 34, ROW_GAP = 70, GROUP_GAP = 40, CW = 200;
  const LS_OFF = 'gits.arch.offsets';

  const esc = s => String(s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
  const alpha = (rgb, a) => { const m = String(rgb).match(/[\d.]+/g); return m ? `rgba(${m[0]},${m[1]},${m[2]},${a})` : rgb; };
  const hexRgb = h => { const n = parseInt(h.slice(1), 16); return `rgb(${n >> 16 & 255}, ${n >> 8 & 255}, ${n & 255})`; };
  // смешать цвет слоя с фоном темы: Cytoscape не применяет альфу из rgba, поэтому заливка считается заранее
  const mix = (fg, bg, k) => { const f = String(fg).match(/[\d.]+/g).map(Number), b = String(bg).match(/[\d.]+/g).map(Number); return `rgb(${[0, 1, 2].map(i => Math.round(f[i] * k + b[i] * (1 - k))).join(',')})`; };
  const FALLBACK = {p: 'rgb(99,102,241)', b1: 'rgb(255,255,255)', b2: 'rgb(243,244,246)', b3: 'rgb(209,213,219)', bc: 'rgb(31,41,55)'};

  function template() {
    return `<div class="flex flex-wrap items-center gap-2 mb-2">
        <button class="btn btn-sm" data-arch="collapse">Свернуть всё</button>
        <button class="btn btn-sm" data-arch="expand">Развернуть всё</button>
        <button class="btn btn-sm btn-ghost" data-arch="fit">По центру</button>
        <button class="btn btn-sm btn-ghost" data-arch="reset" title="Вернуть блоки на исходные места">Сбросить расположение</button>
        <label class="label cursor-pointer gap-2 py-0"><span class="label-text text-xs">подписи стрелок</span><input type="checkbox" class="toggle toggle-sm" data-arch="labels" checked></label>
        <span class="text-xs opacity-60 ml-auto">Колесо: масштаб. Фон: сдвиг. Блок можно перетащить. Клик: описание, блок разворачивается.</span>
      </div>
      <div class="flex flex-wrap items-center gap-x-4 gap-y-1 mb-2 text-xs" data-arch="legend"></div>
      <div class="flex flex-1 min-h-0 rounded-box border border-base-300 overflow-hidden" data-arch="ws" style="--sw:380px">
        <div class="flex-1 min-w-0 bg-base-100" data-arch="canvas" style="min-height:360px"></div>
        <div data-arch="split" class="flex-none cursor-col-resize relative bg-base-300 hover:bg-primary" style="width:9px;touch-action:none" title="Потяните, чтобы изменить ширину описания; двойной клик возвращает"></div>
        <aside class="flex-none overflow-auto p-3 bg-base-100 text-sm" data-arch="side" style="width:var(--sw)"><div class="opacity-60">Нажмите на блок на схеме. Здесь появятся описание, где этот код лежит сейчас, связи и, если блок составной, его внутренняя структура.</div></aside>
      </div>`;
  }

  function mount(root, opts = {}) {
    const D = window.ARCH_DATA;
    if (!D) { root.innerHTML = '<div class="p-4 opacity-60">Нет данных схемы (architecture-data.js).</div>'; return null; }
    if (!window.cytoscape) { root.innerHTML = '<div class="p-4 opacity-60">Не загрузилась библиотека графа (cytoscape).</div>'; return null; }
    root.innerHTML = template();
    const q = n => root.querySelector(`[data-arch="${n}"]`);
    const canvas = q('canvas'), side = q('side'), split = q('split'), ws = q('ws');
    const groupById = {}, nodeById = {}, owner = {};
    D.groups.forEach(g => { groupById[g.id] = g; g.nodes.forEach(n => { nodeById[n.id] = n; owner[n.id] = g.id; }); });
    const macro = {}; D.macroLabels.forEach(([a, b, l]) => { macro[a + '|' + b] = l; });
    const expanded = new Set();
    let selected = null, showLabels = true, userMoved = false, base = {};
    let off = {}; try { off = JSON.parse(localStorage.getItem(LS_OFF) || '{}'); } catch (e) { off = {}; }
    const saveOff = () => { try { localStorage.setItem(LS_OFF, JSON.stringify(off)); } catch (e) {} };

    const colors = () => Object.assign({}, FALLBACK, opts.colors ? opts.colors() : {});
    const titleOf = id => nodeById[id] ? nodeById[id].t : (groupById[id] ? groupById[id].title : id);
    const tagOf = id => nodeById[id] ? nodeById[id].tag : groupById[id].tag;
    const newOf = id => nodeById[id] ? nodeById[id].new : !!groupById[id].new;

    function styleFor(c) {
      const st = [
        {selector: 'node', style: {shape: 'round-rectangle', label: 'data(label)', 'text-wrap': 'wrap', 'text-max-width': NW - 14, 'text-valign': 'center', 'text-halign': 'center',
          'font-size': 12, 'font-family': 'inherit', color: c.bc, width: NW, height: NH, 'background-color': c.b2, 'border-width': 2, 'border-color': c.b3,
          'transition-property': 'opacity, border-width', 'transition-duration': '150ms'}},
        {selector: 'node:parent', style: {'text-valign': 'top', 'text-halign': 'center', 'font-weight': 700, 'font-size': 13, padding: PAD, 'background-opacity': 0.06,
          'background-color': c.bc, 'border-width': 1.5, 'border-color': alpha(c.bc, 0.4), width: 'auto', height: 'auto', 'z-compound-depth': 'bottom', 'text-margin-y': -2}},
        {selector: 'node.macro', style: {'font-weight': 600, 'border-width': 2.5, width: CW}},
        {selector: 'node.new', style: {'border-style': 'dashed'}},
        {selector: 'node:selected', style: {'border-width': 4, 'border-color': c.p, 'overlay-opacity': 0}},
        {selector: 'node.hover', style: {'border-width': 3.5, 'border-color': c.p}},
        {selector: 'node.dim', style: {opacity: 0.22}},
        {selector: 'edge', style: {width: 1.8, 'curve-style': 'bezier', 'target-arrow-shape': 'triangle', 'arrow-scale': 1.1, 'line-color': 'data(color)', 'target-arrow-color': 'data(color)',
          label: 'data(label)', 'font-size': 10, 'font-family': 'inherit', color: c.bc, 'text-background-color': c.b1, 'text-background-opacity': 0.9, 'text-background-padding': '2px',
          opacity: 0.85, 'edge-distances': 'node-position'}},
        {selector: 'edge.dashed', style: {'line-style': 'dashed'}},
        {selector: 'edge.hot', style: {width: 3.4, opacity: 1}},
        {selector: 'edge.dim', style: {opacity: 0.07}},
      ];
      for (const t in D.layers) st.push({selector: `node.tag-${t}`, style: {'background-color': mix(hexRgb(D.layers[t][1]), c.b1, 0.16), 'background-opacity': 1, 'border-color': D.layers[t][1], color: c.bc}});
      return st;
    }

    // легенда: цвета слоёв (рамка и заливка блока), виды стрелок, пунктирная рамка у блоков, которые нужно выделить
    q('legend').innerHTML =
      Object.values(D.layers).map(l => `<span class="inline-flex items-center gap-1"><span style="display:inline-block;width:16px;height:12px;border-radius:3px;border:2px solid ${l[1]};background:${l[1]}33"></span><b>${esc(l[0])}</b></span>`).join('') +
      '<span class="opacity-40">|</span>' +
      Object.values(D.edgeKinds).map(k => `<span class="inline-flex items-center gap-1"><span style="display:inline-block;width:24px;border-top:3px ${k[2] ? 'dashed' : 'solid'} ${k[1]}"></span>${esc(k[0])}</span>`).join('') +
      (D.groups.some(g => g.new || g.nodes.some(n => n.new)) ? '<span class="opacity-40">|</span><span class="inline-flex items-center gap-1"><span style="display:inline-block;width:16px;height:12px;border-radius:3px;border:2px dashed currentColor"></span>нужно выделить</span>' : '');

    const cy = cytoscape({container: canvas, elements: [], style: styleFor(colors()), wheelSensitivity: 0.25, minZoom: 0.15, maxZoom: 3, boxSelectionEnabled: false});

    // ───── раскладка: поток сверху вниз по строкам ─────
    function layout() {
      const rows = {};
      D.groups.forEach(g => (rows[g.row] = rows[g.row] || []).push(g));
      const keys = Object.keys(rows).map(Number).sort((a, b) => a - b);
      const dim = {};
      D.groups.forEach(g => {
        if (expanded.has(g.id)) {
          const cols = g.nodes.length >= 6 ? 2 : 1, per = Math.ceil(g.nodes.length / cols);
          dim[g.id] = {cols, per, w: cols * NW + (cols - 1) * 14 + 2 * PAD, h: TOP_PAD + per * (NH + GAP) - GAP + PAD};
        } else dim[g.id] = {w: CW, h: NH};
      });
      const rowW = {}, rowH = {};
      keys.forEach(r => {
        rowW[r] = rows[r].reduce((t, g) => t + dim[g.id].w, 0) + GROUP_GAP * (rows[r].length - 1);
        rowH[r] = Math.max(...rows[r].map(g => dim[g.id].h));
      });
      const W = Math.max(...Object.values(rowW));
      const pos = {};
      let y = 0;
      keys.forEach(r => {
        let x = (W - rowW[r]) / 2;
        rows[r].forEach(g => {
          const d = dim[g.id];
          if (expanded.has(g.id)) {
            g.nodes.forEach((n, i) => {
              const c = Math.floor(i / d.per), k = i % d.per;
              pos[n.id] = {x: x + PAD + c * (NW + 14) + NW / 2, y: y + TOP_PAD + k * (NH + GAP) + NH / 2};
            });
          } else pos[g.id] = {x: x + CW / 2, y: y + (rowH[r] - NH) / 2 + NH / 2};
          x += d.w + GROUP_GAP;
        });
        y += rowH[r] + ROW_GAP;
      });
      return pos;
    }

    const visId = id => expanded.has(owner[id]) ? id : owner[id];
    function computeEdges() {
      const seen = {}, out = [];
      for (const [a, b, kind, label] of D.edges) {
        const ra = visId(a), rb = visId(b);
        if (ra === rb) continue;
        const oa = owner[a], ob = owner[b], both = expanded.has(oa) && expanded.has(ob);
        if (!both && oa === 'g_dec' && ob === 'g_mem' && !expanded.has('g_dec') && !expanded.has('g_mem')) continue;
        const key = both ? ra + '|' + rb + '|' + kind : ra + '|' + rb;
        if (seen[key]) continue; seen[key] = 1;
        out.push({source: ra, target: rb, kind, label: both ? label : (macro[oa + '|' + ob] || '')});
      }
      if (!expanded.has('g_src')) out.push({source: expanded.has('g_out') ? 'hubout' : 'g_out', target: 'g_src', kind: 'ctl', label: 'речь, команды, режим'});
      return out;
    }

    const nodeLabel = id => {
      const g = groupById[id], L = D.layers[tagOf(id)][0];
      return `${titleOf(id)}\n${L}${g ? ` · ${g.nodes.length} ч.` : ''}${newOf(id) ? ' · нужно выделить' : ''}`;
    };

    function rebuild(keepView) {
      const pos = layout();
      base = pos;
      const els = [];
      D.groups.forEach(g => {
        if (expanded.has(g.id)) {
          els.push({group: 'nodes', data: {id: 'g:' + g.id, label: g.title, gid: g.id}, classes: 'grp'});
          g.nodes.forEach(n => {
            const o = off[n.id] || [0, 0];
            els.push({group: 'nodes', data: {id: n.id, parent: 'g:' + g.id, label: nodeLabel(n.id)}, position: {x: pos[n.id].x + o[0], y: pos[n.id].y + o[1]}, classes: `tag-${n.tag}${n.new ? ' new' : ''}`});
          });
        } else {
          const o = off[g.id] || [0, 0];
          els.push({group: 'nodes', data: {id: g.id, label: nodeLabel(g.id)}, position: {x: pos[g.id].x + o[0], y: pos[g.id].y + o[1]}, classes: `macro tag-${g.tag}${g.new ? ' new' : ''}`});
        }
      });
      computeEdges().forEach((e, i) => {
        const k = D.edgeKinds[e.kind];
        els.push({group: 'edges', data: {id: 'e' + i, source: e.source, target: e.target, label: showLabels ? e.label : '', color: k[1]}, classes: k[2] ? 'dashed' : ''});
      });
      cy.batch(() => { cy.elements().remove(); cy.add(els); });
      if (selected) cy.getElementById(selected).select();
      if (!keepView || !userMoved) fitView();
    }

    function fitView() { cy.resize(); cy.fit(undefined, 28); userMoved = false; }
    function persistOffsets() {
      cy.nodes(':childless').forEach(n => {
        const b = base[n.id()];
        if (!b) return;
        const dx = Math.round(n.position('x') - b.x), dy = Math.round(n.position('y') - b.y);
        if (dx || dy) off[n.id()] = [dx, dy]; else delete off[n.id()];
      });
      saveOff();
    }

    // ───── панель описания ─────
    const pill = t => `<span class="badge badge-sm text-white border-0" style="background:${D.layers[t][1]}">${esc(D.layers[t][0])}</span>`;
    function conns(ids) {
      const inc = {}, outc = {};
      for (const [a, b, , label] of D.edges) {
        if (ids.has(a) && !ids.has(b)) (outc[groupById[owner[b]].title + ' / ' + titleOf(b)] ||= new Set()).add(label || '');
        if (ids.has(b) && !ids.has(a)) (inc[groupById[owner[a]].title + ' / ' + titleOf(a)] ||= new Set()).add(label || '');
      }
      const f = o => Object.entries(o).map(([k, v]) => [k, [...v].filter(Boolean)]);
      return [f(inc), f(outc)];
    }
    const list = l => l.length ? '<ul class="list-disc pl-5">' + l.map(([n, ls]) => `<li>${esc(n)}${ls.length ? ` <span class="opacity-60">(${ls.map(esc).join(', ')})</span>` : ''}</li>`).join('') + '</ul>' : '<span class="opacity-60">нет</span>';
    const sec = (t, body) => `<div class="border-t border-dashed border-base-300 mt-3 pt-2"><div class="text-xs uppercase opacity-60 mb-1">${t}</div>${body}</div>`;

    function openInfo(id) {
      const g = groupById[id], n = nodeById[id];
      if (!g && !n) return;
      let h = `<div class="text-lg font-semibold">${esc(g ? g.title : n.t)}</div><div class="flex flex-wrap items-center gap-2 my-1">${pill(g ? g.tag : n.tag)}` +
        `${(g ? g.new : n.new) ? '<span class="text-error text-xs font-semibold">нужно выделить (сейчас такого компонента нет)</span>' : ''}` +
        `${n ? `<span class="text-xs opacity-60">часть блока «${esc(groupById[owner[id]].title)}»</span>` : ''}</div>`;
      h += `<p>${esc(g ? g.summary : n.do)}</p>`;
      const o = g ? g.out : n.out; if (o) h += `<p class="mt-1"><b>Отдаёт:</b> ${esc(o)}</p>`;
      if (n && n.now) h += `<p class="mt-1 text-xs font-mono opacity-60">сейчас: ${esc(n.now)}</p>`;
      const ids = g ? new Set(g.nodes.map(x => x.id)) : new Set([id]);
      if (g) {
        h += sec(`Внутренняя структура (${g.nodes.length})`, g.nodes.map(p =>
          `<div class="rounded-lg border border-base-300 p-2 my-1 cursor-pointer hover:border-primary" data-part="${p.id}" style="border-left:5px solid ${D.layers[p.tag][1]}"><b>${esc(p.t)}</b> ${pill(p.tag)}${p.new ? ' <span class="text-error text-xs">нужно выделить</span>' : ''}` +
          `<div>${esc(p.do)}</div>${p.out ? `<div class="text-xs opacity-60">→ ${esc(p.out)}</div>` : ''}${p.now ? `<div class="text-xs font-mono opacity-60">сейчас: ${esc(p.now)}</div>` : ''}</div>`).join(''));
        const internal = D.edges.filter(([a, b]) => ids.has(a) && ids.has(b));
        if (internal.length) h += sec('Как части связаны между собой', '<ul class="list-disc pl-5">' + internal.map(([a, b, , l]) => `<li>${esc(titleOf(a))} → ${esc(titleOf(b))}${l ? ` <span class="opacity-60">(${esc(l)})</span>` : ''}</li>`).join('') + '</ul>');
      }
      const [inc, outc] = conns(ids);
      h += sec('Получает от', list(inc)) + sec('Отдаёт в', list(outc));
      const gid = g ? id : owner[id];
      h += `<div class="mt-3"><button class="btn btn-sm btn-outline btn-primary" data-toggle="${gid}">${expanded.has(gid) ? 'Свернуть' : 'Развернуть'} блок «${esc(groupById[gid].title)}»</button></div>`;
      side.innerHTML = h; side.scrollTop = 0;
      side.querySelectorAll('[data-part]').forEach(p => p.onclick = () => { expanded.add(owner[p.dataset.part]); selected = p.dataset.part; rebuild(true); openInfo(p.dataset.part); });
      side.querySelectorAll('[data-toggle]').forEach(b => b.onclick = () => { const t = b.dataset.toggle; expanded.has(t) ? expanded.delete(t) : expanded.add(t); selected = t; rebuild(true); openInfo(t); });
    }

    // ───── события ─────
    cy.on('tap', 'node', e => {
      const n = e.target;
      if (n.hasClass('grp')) { selected = n.data('gid'); openInfo(selected); return; }
      const id = n.id();
      if (groupById[id]) { expanded.add(id); selected = id; rebuild(true); openInfo(id); } else { selected = id; openInfo(id); }
    });
    cy.on('dbltap', 'node.grp', e => { const id = e.target.data('gid'); expanded.delete(id); selected = id; rebuild(true); openInfo(id); });
    cy.on('mouseover', 'node', e => {
      const n = e.target; if (n.hasClass('grp')) return;
      n.addClass('hover'); canvas.style.cursor = 'pointer';
      const nb = n.closedNeighborhood();
      cy.elements().not(nb).not(':parent').addClass('dim'); n.connectedEdges().addClass('hot');
    });
    cy.on('mouseout', 'node', e => { e.target.removeClass('hover'); canvas.style.cursor = ''; cy.elements().removeClass('dim hot'); });
    cy.on('dragfree', 'node', persistOffsets);
    cy.on('pan zoom', () => { userMoved = true; });
    if (window.ResizeObserver) new ResizeObserver(() => { if (canvas.offsetWidth) { cy.resize(); if (!userMoved) cy.fit(undefined, 28); } }).observe(canvas);

    q('collapse').onclick = () => { expanded.clear(); selected = null; rebuild(false); };
    q('expand').onclick = () => { D.groups.forEach(g => expanded.add(g.id)); selected = null; rebuild(false); };
    q('fit').onclick = () => fitView();
    q('reset').onclick = () => { off = {}; saveOff(); rebuild(false); };
    q('labels').onchange = e => { showLabels = e.target.checked; rebuild(true); };

    // подвижная граница между схемой и описанием
    let sw = 380; try { const v = parseInt(localStorage.getItem('gits.arch.sideW') || '0'); if (v >= 240 && v <= 900) sw = v; } catch (e) {}
    const setSW = (v, limit) => { const max = limit && ws.clientWidth > 700 ? ws.clientWidth - 320 : 900; sw = Math.max(240, Math.min(max, v)); ws.style.setProperty('--sw', sw + 'px'); cy.resize(); };
    setSW(sw, false);
    split.addEventListener('pointerdown', e => {
      split.setPointerCapture(e.pointerId);
      const move = ev => setSW(ws.getBoundingClientRect().right - ev.clientX - 4, true);
      const up = () => { split.releasePointerCapture(e.pointerId); split.removeEventListener('pointermove', move); split.removeEventListener('pointerup', up); try { localStorage.setItem('gits.arch.sideW', String(sw)); } catch (x) {} };
      split.addEventListener('pointermove', move); split.addEventListener('pointerup', up);
    });
    split.addEventListener('dblclick', () => setSW(380, false));

    rebuild(false);
    const api = {cy, fit: fitView, rebuild, expanded};
    window.ARCH.last = api;
    return api;
  }

  window.ARCH = {mount, template};
})();
