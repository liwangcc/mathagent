/* MathAgent lesson flow v1 — presentation layer only.
   Re-sequences the chapter's EXISTING blocks into a teacher-paced rhythm:
   引入 → 探究 → 归纳 → 练习 (motivate → explore → formalize → practice).
   It never writes mathematical state and never calls scene/model functions
   except the runtime's own scene navigation. Every formula, definition and
   answer shown in a stage is the node the chapter already rendered; the only
   text added here is generic classroom guidance (stage names and prompts).
   Stages are views, not gates: any stage and the full workbench (总览) are
   one click or key away. Works identically for the WebGL and SVG paths.
   Only nodes the runtime reaches by global id are moved; anything it finds
   relative to a parent (e.g. a tool row's hint) is mirrored, never moved. */
(function () {
  'use strict';
  if (window.LessonFlow) return;
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const text = el => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');
  const STAGES = [
    { id: 'motivate', n: 1, name: '引入', cue: '先看图：说一说图中每种颜色、每个标注表示什么。' },
    { id: 'explore', n: 2, name: '探究', cue: '动手试：改变一个量，看图、式子和数值怎样一起变。' },
    { id: 'formalize', n: 3, name: '归纳', cue: '说规律：用定义把发现说完整，注意成立条件。' },
    { id: 'practice', n: 4, name: '练习', cue: '试一试：先独立作答，再检查或看答案。' },
  ];

  /* Each adapter only names existing DOM of one runtime family.
     blocks: key → { el, keep (fold in place of leaving layout), open (details) }
     stages: stage → ordered block keys shown in the stage card
     pins:   [element, destination] moved into always-visible chrome in lesson mode */
  const ADAPTERS = {
    primary: {
      detect: () => !!(window.CW && window.CW.app && $('#scene-nav') && $('.info-panel')),
      sceneCount: () => window.CW.app.scenes.length,
      sceneIndex: () => window.CW.app.current,
      sceneName: i => window.CW.app.scenes[i]?.title || '',
      goScene: i => window.CW.app.go(i),
      watch: () => [$('#scene-title')],
      live: () => $('#announcer'),
      heading: () => $('.scene-heading'),
      card: () => $('.info-panel'),
      figurePanel: () => $('.model-panel'),
      math: () => { try { return JSON.stringify(window.CW.snapshot().state); } catch (_) { return null; } },
      tools: () => [$('#fraction-tools'), $('#canvas-view-tools')].filter(Boolean),
      toolsAnchor: () => $('.model-top'),
      hint: () => $('#canvas-gesture-hint'),
      hintAnchor: () => $('.model-top'),
      // Figure keeps its aspect and is sized to the first screen under its real top edge.
      fit: { el: '#diagram', below: ['#timeline'], min: 300 },
      blocks: {
        look: { el: () => $$('.info-panel > .info-block')[1] },
        definition: { el: () => $$('.info-panel > .info-block')[0] },
        current: { el: () => $('.info-panel > .info-block.current') },
        controls: { el: () => $('#controls'), keep: true },
        note: { el: () => $('#figure-note') },
        exercise: { el: () => $('#exercise'), open: true },
      },
      stages: {
        motivate: ['look'],
        explore: ['controls', 'current', 'note'],
        formalize: ['definition', 'current'],
        practice: ['exercise'],
      },
    },
    junior: {
      detect: () => !!(window.MathAgent && $('#sceneNav [data-scene]') && $('aside.reading')),
      nav: () => $$('#sceneNav [data-scene]'),
      sceneCount() { return this.nav().length; },
      sceneIndex() { return Math.max(0, this.nav().findIndex(b => b.classList.contains('active'))); },
      sceneName(i) { return text(this.nav()[i]?.querySelector('.scene-name')) || text(this.nav()[i]); },
      goScene(i) { this.nav()[i]?.click(); },
      watch: () => [$('#sceneTitle')],
      live: () => $('#live'),
      heading: () => $('.scene-heading'),
      card: () => $('aside.reading'),
      figurePanel: () => $('.figure-card'),
      math: () => { try { return JSON.stringify(window.MathAgent.mathState); } catch (_) { return null; } },
      blocks: {
        look: { el: () => $('#mapping') },
        select: { el: () => $('#selectedNote') },
        demo: { el: () => $('section.timeline') },
        controls: { el: () => $('.controls-card'), keep: true },
        label: { el: () => $('aside.reading > .eyebrow') },
        definition: { el: () => $('#definition') },
        formula: { el: () => $('#formula') },
        readings: { el: () => $('#readings') },
        feedback: { el: () => $('#feedback') },
        boundary: { el: () => $('#counterexample') },
        snapshot: { el: () => $('#snapshot') },
        exercise: { el: () => $('#exerciseDetails'), open: true },
      },
      stages: {
        motivate: ['look', 'select', 'demo'],
        explore: ['controls', 'formula', 'readings', 'feedback', 'select', 'demo', 'snapshot'],
        formalize: ['label', 'definition', 'formula', 'readings', 'boundary'],
        practice: ['exercise'],
      },
      // 撤销 / 恢复本场景 stay one click away in every stage (they live below the fold otherwise).
      pins: () => [[$('#undo'), $('.top-tools')], [$('#resetScene'), $('.top-tools')]],
    },
    senior: {
      detect: () => !!(window.CourseApp && $('#tabs [data-scene]') && $('aside.lesson')),
      nav: () => $$('#tabs [data-scene]'),
      sceneCount() { return this.nav().length; },
      sceneIndex() {
        let id = null; try { id = window.CourseApp.getState().scene; } catch (_) {}
        const i = this.nav().findIndex(b => (id ? b.dataset.scene === id : b.classList.contains('active')));
        return Math.max(0, i);
      },
      sceneName(i) { const b = this.nav()[i]; if (!b) return ''; const c = b.cloneNode(true); c.querySelectorAll('small').forEach(x => x.remove()); return text(c); },
      goScene(i) { this.nav()[i]?.click(); },
      watch: () => [$('#sceneTitle')],
      live: () => $('#live'),
      heading: () => $('.scenehead'),
      card: () => $('aside.lesson'),
      figurePanel: () => $('section.board'),
      math: () => { try { return JSON.stringify(window.CourseApp.getState().math); } catch (_) { return null; } },
      blocks: {
        definition: { el: () => $$('aside.lesson > .sect')[0] },
        look: { el: () => $$('aside.lesson > .sect')[1] },
        facts: { el: () => $$('aside.lesson > .sect')[2] },
        metrics: { el: () => $('#metrics') },
        controls: { el: () => $('#controls'), keep: true },
        demo: { el: () => $('section.animation') },
        exercise: { el: () => $('#exercise'), open: true },
      },
      stages: {
        motivate: ['look', 'demo'],
        explore: ['controls', 'metrics', 'facts', 'demo'],
        formalize: ['definition', 'facts'],
        practice: ['exercise'],
      },
    },
    // H16 ships its own constrained runtime (window.H16Courseware) with the same lesson sections.
    h16: {
      detect: () => !!(window.H16Courseware && $('.tabs [data-scene]') && $('aside.lesson')),
      nav: () => $$('.tabs [data-scene]'),
      sceneCount() { return this.nav().length; },
      sceneIndex() {
        let id = null; try { id = window.H16Courseware.getState().scene; } catch (_) {}
        return Math.max(0, this.nav().findIndex(b => (id ? b.dataset.scene === id : b.getAttribute('aria-selected') === 'true')));
      },
      sceneName(i) { return text(this.nav()[i]?.querySelector('span')) || text(this.nav()[i]); },
      goScene(i) { this.nav()[i]?.click(); },
      watch: () => [$('#sceneTitle')],
      live: () => $('#live'),
      heading: () => $('.scene-line'),
      card: () => $('aside.lesson'),
      figurePanel: () => $('section.board-card'),
      math: () => { try { return JSON.stringify(window.H16Courseware.getState().math); } catch (_) { return null; } },
      blocks: {
        definition: { el: () => $$('aside.lesson > .lesson-section')[0] },
        look: { el: () => $$('aside.lesson > .lesson-section')[1] },
        facts: { el: () => $$('aside.lesson > .lesson-section')[2] },
        metrics: { el: () => $('#metrics') },
        controls: { el: () => $('#controls'), keep: true },
        demo: { el: () => $('section.animation') },
        exercise: { el: () => $('#exercise'), open: true },
      },
      stages: {
        motivate: ['look', 'demo'],
        explore: ['controls', 'metrics', 'facts', 'demo'],
        formalize: ['definition', 'facts'],
        practice: ['exercise'],
      },
    },
    // Single-scene WebGL demo (高中/空间几何关系): the canvas stays put under the stage head;
    // each step shows the page's own controls for that step below it.
    space: {
      detect: () => !!($('[data-rel]') && $('#s-fallback') && $('main .board')),
      sceneCount: () => 1,
      sceneIndex: () => 0,
      sceneName: () => text($('main h1')),
      goScene() {},
      watch: () => [],
      live: () => $('#lf-live'),
      heading: () => $('main > section'),
      card: () => $$('main .board')[0],
      figurePanel: () => $$('main .board')[0],
      math: () => JSON.stringify({ relation: ($('[data-rel].active') || {}).dataset?.rel || null }),
      fit: { el: '#s', below: [], min: 320 },
      cues: {
        explore: '动手试：切换线线、线面、面面关系，拖动旋转，从不同角度看位置关系。',
        formalize: '说规律：先猜一下，再显示规律，把位置关系说完整。',
      },
      blocks: {
        look: { el: () => $('#legend') },
        relation: { el: () => $('#o') },
        lines: { el: () => $$('main .board .group')[0] },
        lineplane: { el: () => $$('main .board .group')[1] },
        planes: { el: () => $$('main .board .group')[2] },
        lock: { el: () => $('#lock') },
        reset: { el: () => $('#reset') },
        guess: { el: () => $('#guess') },
        show: { el: () => $('#show') },
        rule: { el: () => $('#a') },
        exercise: { el: () => $$('main .board')[1], openInside: 'details' },
      },
      stages: {
        motivate: ['relation', 'look'],
        explore: ['relation', 'lines', 'lineplane', 'planes', 'lock', 'reset'],
        formalize: ['relation', 'guess', 'show', 'rule'],
        practice: ['exercise'],
      },
    },
  };

  const LF = {
    stages: STAGES, on: false, stage: 'motivate', family: null, a: null, parked: new Map(), pinned: [], sceneSeen: -1,
    init() {
      const fam = Object.keys(ADAPTERS).find(k => { try { return ADAPTERS[k].detect(); } catch (_) { return false; } });
      if (!fam) return false;
      this.family = fam; this.a = ADAPTERS[fam];
      document.body.dataset.lfFamily = fam;
      this.build();
      let pref = null; try { pref = localStorage.getItem('mathagent.lessonflow'); } catch (_) {}
      const q = new URLSearchParams(location.search).get('lesson');
      this.setOn(q ? q !== 'off' : pref !== 'off', false);
      const mo = new MutationObserver(() => this.sceneChanged());
      this.a.watch().filter(Boolean).forEach(el => mo.observe(el, { childList: true, characterData: true, subtree: true }));
      this.sceneChanged(true);
      document.addEventListener('keydown', e => this.key(e));
      window.addEventListener('resize', () => this.queueFit());
      // The chapter's own 减少动态 switch also removes stage transitions (OS preference is handled in CSS).
      const rc = $('#reduced') || $('#reduce') || $('#reduceMotion');
      if (rc) { const sync = () => document.body.classList.toggle('lf-reduce', rc.checked); rc.addEventListener('change', sync); sync(); }
      const fp = this.a.figurePanel && this.a.figurePanel();
      if (fp) new MutationObserver(() => this.queueFit()).observe(fp, { attributes: true, attributeFilter: ['hidden'], subtree: true });
      return true;
    },
    build() {
      const a = this.a;
      if (a.live && !a.live() && a.sceneCount() === 1) {
        const l = document.createElement('p');
        l.id = 'lf-live'; l.className = 'lf-sr'; l.setAttribute('role', 'status'); l.setAttribute('aria-live', 'polite');
        document.body.append(l);
      }
      const card = a.card();
      card.classList.add('lf-card');
      const head = document.createElement('div');
      head.className = 'lf-card-head'; head.id = 'lf-card-head';
      head.innerHTML = '<span class="lf-num" aria-hidden="true"></span><p class="lf-cue-line"><strong class="lf-title"></strong><span class="lf-cue"></span></p>';
      card.prepend(head);
      this.head = head;
      const rail = document.createElement('nav');
      rail.className = 'lf-rail'; rail.id = 'lf-rail';
      rail.setAttribute('aria-label', '课堂节奏：引入、探究、归纳、练习（翻页笔或 PageDown 下一步）');
      rail.innerHTML = '<ol class="lf-steps">' +
        STAGES.map(s => `<li><button type="button" class="lf-step" data-lf-stage="${s.id}" data-lf-n="${s.n}"><b>${s.n}</b>${s.name}</button></li>`).join('') +
        '</ol><div class="lf-move"><button type="button" id="lf-prev" class="lf-prev">◀ 上一步</button><button type="button" id="lf-next" class="lf-next">下一步 ▶</button></div>' +
        '<button type="button" id="lf-overview" class="lf-overview" aria-pressed="false" title="显示完整工作台：全部参数、定义与练习同屏">总览</button>' +
        '<button type="button" id="lf-help" class="lf-help" aria-expanded="false" aria-controls="lf-help-panel" aria-label="课堂操作帮助">?</button>' +
        '<div id="lf-help-panel" class="lf-help-panel" role="dialog" aria-label="课堂操作帮助" hidden><strong tabindex="-1">课堂节奏怎么用</strong><ul>' +
        '<li><kbd>PageDown</kbd> / 翻页笔「下一页」：下一步；<kbd>PageUp</kbd>：上一步。练习之后自动进入下一个场景。</li>' +
        '<li>数字键 <kbd>1</kbd>–<kbd>4</kbd>：直接跳到引入、探究、归纳、练习（输入框和图上不生效）。</li>' +
        '<li>四个步骤随时可点，不设门槛；「总览」恢复完整工作台，再点一次回到课堂节奏。</li>' +
        '<li>切换步骤只改变显示重点，不改变任何数学参数；图上拖动、方向键、滚轮缩放在每一步都可用。</li>' +
        '<li><kbd>Esc</kbd> 关闭本帮助。</li></ul></div>';
      a.heading().append(rail);
      this.rail = rail;
      rail.addEventListener('click', e => {
        const b = e.target.closest('button'); if (!b) return;
        if (b.dataset.lfStage) this.go(b.dataset.lfStage, true);
        else if (b.id === 'lf-prev') this.prev();
        else if (b.id === 'lf-next') this.next();
        else if (b.id === 'lf-overview') this.setOn(!this.on, true);
        else if (b.id === 'lf-help') this.help();
      });
      // Tool rows (zoom / free board) fold behind one toggle; folded rows stay
      // laid out (inert, zero height) so in-figure editors keep every source.
      const tools = a.tools ? a.tools() : [];
      if (tools.length) {
        const drawer = document.createElement('div');
        drawer.className = 'lf-tools'; drawer.id = 'lf-tools';
        tools[0].before(drawer);
        tools.forEach(t => drawer.append(t));
        const tbtn = document.createElement('button');
        tbtn.type = 'button'; tbtn.id = 'lf-tools-toggle'; tbtn.className = 'lf-tools-toggle';
        tbtn.setAttribute('aria-expanded', 'false'); tbtn.setAttribute('aria-controls', 'lf-tools');
        tbtn.textContent = '缩放与工具 ▾';
        tbtn.onclick = () => this.tools(!document.body.classList.contains('lf-tools-open'));
        a.toolsAnchor()?.append(tbtn);
        this.drawer = drawer; this.tbtn = tbtn;
      }
      // The runtime owns its gesture hint (and finds it relative to its tool
      // row), so the hint is mirrored as text, never moved.
      const hint = a.hint ? a.hint() : null;
      if (hint) {
        const mirror = document.createElement('span');
        mirror.className = 'lf-hint'; mirror.setAttribute('aria-hidden', 'true');
        const copy = () => { mirror.textContent = hint.textContent; };
        copy(); new MutationObserver(copy).observe(hint, { childList: true, characterData: true, subtree: true });
        (a.hintAnchor() || hint.parentNode).insertBefore(mirror, this.tbtn && this.tbtn.parentNode === a.hintAnchor() ? this.tbtn : null);
      }
      for (const [k, def] of Object.entries(a.blocks)) {
        const el = def.el();
        if (!el) continue;
        el.dataset.lfBlock = k;
        if (def.keep) el.classList.add('lf-keep');
        const mark = document.createComment('lf:' + k);
        el.before(mark);
        this.parked.set(k, { el, mark, def });
      }
      for (const [el, to] of (a.pins ? a.pins() : [])) {
        if (!el || !to) continue;
        const mark = document.createComment('lf:pin');
        el.before(mark);
        this.pinned.push({ el, mark, to });
      }
    },
    // an adapter may word a step's cue for its own controls (same four steps)
    cue(s) { return (this.a.cues && this.a.cues[s.id]) || s.cue; },
    queueFit() {
      if (this.fitRaf) return;
      this.fitRaf = requestAnimationFrame(() => { this.fitRaf = 0; this.fit(); });
    },
    fit() {
      const f = this.a.fit, body = document.body;
      if (!f || !this.on) { body.style.removeProperty('--lf-fig-h'); this.figH = 0; return; }
      const el = $(f.el); if (!el) return;
      let below = 8;
      for (const sel of f.below || []) { const e = $(sel); if (e && !e.hidden && e.getClientRects().length) below += e.getBoundingClientRect().height; }
      const top = el.getBoundingClientRect().top + window.scrollY;
      const h = Math.max(f.min || 300, Math.floor(window.innerHeight - top - below));
      if (h !== this.figH) { this.figH = h; body.style.setProperty('--lf-fig-h', h + 'px'); window.dispatchEvent(new Event('resize')); }
    },
    tools(open) {
      document.body.classList.toggle('lf-tools-open', open);
      this.tbtn?.setAttribute('aria-expanded', String(open));
      if (this.drawer) this.drawer.inert = this.on && !open;
      if (this.tbtn) this.tbtn.textContent = open ? '缩放与工具 ▴' : '缩放与工具 ▾';
      this.queueFit();
    },
    setOn(on, user) {
      this.on = on;
      document.body.classList.toggle('lf-on', on);
      $('#lf-overview')?.setAttribute('aria-pressed', String(!on));
      if ($('#lf-overview')) $('#lf-overview').textContent = on ? '总览' : '课堂节奏';
      if (user) { try { localStorage.setItem('mathagent.lessonflow', on ? 'on' : 'off'); } catch (_) {} }
      for (const p of this.pinned) { if (on) p.to.append(p.el); else p.mark.after(p.el); p.el.classList.toggle('lf-pin', on); }
      if (on) this.apply(); else this.restore();
      this.tools(document.body.classList.contains('lf-tools-open'));
      if (user) this.say(on ? `课堂节奏：第 ${this.cur().n} 步 ${this.cur().name}` : '总览：完整工作台已显示，数学状态不变。');
      this.fit();
      window.dispatchEvent(new Event('resize'));
    },
    restore() {
      for (const p of this.parked.values()) {
        p.mark.after(p.el);
        p.el.classList.remove('lf-fold', 'lf-show'); p.el.inert = false;
        if (p.def.open) p.el.open = false;
        if (p.def.openInside) { const d = p.el.querySelector(p.def.openInside); if (d) d.open = false; }
      }
      document.body.removeAttribute('data-lf-stage');
    },
    cur() { return STAGES.find(s => s.id === this.stage); },
    apply() {
      if (!this.on) return;
      const s = this.cur(), card = this.a.card(), shown = this.a.stages[s.id] || [];
      document.body.dataset.lfStage = s.id;
      for (const [k, p] of this.parked) {
        if (shown.includes(k)) continue;
        // keep-layout blocks fold inside the card (zero height, inert) instead of leaving the layout
        if (p.def.keep) card.append(p.el); else p.mark.after(p.el);
        p.el.classList.remove('lf-show'); p.el.classList.add('lf-fold');
        p.el.inert = !!p.def.keep;
        if (p.def.open) p.el.open = false;
        if (p.def.openInside) { const d = p.el.querySelector(p.def.openInside); if (d) d.open = false; }
      }
      for (const k of shown) {
        const p = this.parked.get(k); if (!p) continue;
        card.append(p.el);
        p.el.classList.add('lf-show'); p.el.classList.remove('lf-fold'); p.el.inert = false;
        if (p.def.open) p.el.open = true;
        if (p.def.openInside) { const d = p.el.querySelector(p.def.openInside); if (d) d.open = true; }
      }
      this.head.querySelector('.lf-num').textContent = s.n;
      this.head.querySelector('.lf-title').textContent = s.name;
      this.head.querySelector('.lf-cue').textContent = this.cue(s);
      card.classList.remove('lf-enter'); void card.offsetWidth; card.classList.add('lf-enter');
      this.syncRail();
      this.queueFit();
    },
    syncRail() {
      const s = this.cur(), i = this.a.sceneIndex();
      $$('.lf-step', this.rail).forEach(b => {
        const on = b.dataset.lfStage === s.id;
        b.classList.toggle('active', on);
        if (on) b.setAttribute('aria-current', 'step'); else b.removeAttribute('aria-current');
      });
      const next = this.nextTarget(), prev = this.prevTarget();
      const nb = $('#lf-next'), pb = $('#lf-prev');
      nb.disabled = !next; pb.disabled = !prev;
      const nextName = !next ? '' : next.scene !== i ? `下一场景：${this.a.sceneName(next.scene)}` : `下一步：${STAGES.find(x => x.id === next.stage).name}`;
      nb.innerHTML = !next ? '本章已完成' : `<span>${nextName.replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]))}</span> ▶`;
      nb.title = nextName || '已是本章最后一步';
      nb.setAttribute('aria-label', nextName || '本章已完成');
      pb.textContent = !prev ? '◀ 上一步' : prev.scene !== i ? '◀ 上一场景' : `◀ ${STAGES.find(x => x.id === prev.stage).name}`;
      pb.setAttribute('aria-label', !prev ? '上一步（已是第一步）' : prev.scene !== i ? `上一场景：${this.a.sceneName(prev.scene)}` : `上一步：${STAGES.find(x => x.id === prev.stage).name}`);
    },
    nextTarget() {
      const k = STAGES.findIndex(s => s.id === this.stage), i = this.a.sceneIndex();
      if (k < STAGES.length - 1) return { scene: i, stage: STAGES[k + 1].id };
      if (i < this.a.sceneCount() - 1) return { scene: i + 1, stage: STAGES[0].id };
      return null;
    },
    prevTarget() {
      const k = STAGES.findIndex(s => s.id === this.stage), i = this.a.sceneIndex();
      if (k > 0) return { scene: i, stage: STAGES[k - 1].id };
      if (i > 0) return { scene: i - 1, stage: STAGES[STAGES.length - 1].id };
      return null;
    },
    move(t) {
      if (!t) return;
      if (!this.on) this.setOn(true, true);
      if (t.scene !== this.a.sceneIndex()) { this.pending = t.stage; this.a.goScene(t.scene); this.sceneChanged(); }
      else this.go(t.stage, true);
    },
    next() { this.move(this.nextTarget()); },
    prev() { this.move(this.prevTarget()); },
    go(id, user) {
      if (!STAGES.some(s => s.id === id)) return;
      if (!this.on) this.setOn(true, !!user);
      this.stage = id;
      this.apply();
      if (user) { const s = this.cur(); this.say(`第 ${s.n} 步 ${s.name}：${this.cue(s)}`); }
    },
    sceneChanged(first) {
      const i = this.a.sceneIndex();
      if (!first && i === this.sceneSeen) { this.syncRail(); return; }
      this.sceneSeen = i;
      this.stage = this.pending || 'motivate';
      this.pending = null;
      if (this.on) this.apply(); else this.syncRail();
    },
    say(msg) {
      const live = this.a.live(); if (!live) return;
      clearTimeout(this.sayTimer);
      this.sayTimer = setTimeout(() => { live.textContent = msg; }, 60);
    },
    help(force) {
      const p = $('#lf-help-panel'), b = $('#lf-help');
      const open = force === undefined ? p.hidden : force;
      p.hidden = !open; b.setAttribute('aria-expanded', String(open));
      if (open) p.querySelector('strong').focus();
    },
    key(e) {
      if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey) return;
      const t = e.target;
      if (e.key === 'Escape' && !$('#lf-help-panel').hidden) { this.help(false); $('#lf-help').focus(); return; }
      const typing = t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.getAttribute?.('role') === 'slider');
      if (typing) return;
      if (e.key === 'PageDown') { e.preventDefault(); this.next(); }
      else if (e.key === 'PageUp') { e.preventDefault(); this.prev(); }
      else if (/^[1-4]$/.test(e.key) && !(t && t.closest && t.closest('svg, canvas, #figure-wrap, .plot-host, .board'))) {
        e.preventDefault(); this.go(STAGES[+e.key - 1].id, true);
      }
    },
    snapshot() {
      const vis = el => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden' && !el.classList.contains('lf-fold');
      return {
        family: this.family, on: this.on, stage: this.stage, scene: this.a.sceneIndex(), scenes: this.a.sceneCount(),
        next: $('#lf-next')?.textContent || '', shown: [...this.parked.keys()].filter(k => vis(this.parked.get(k).el)),
        title: this.head?.querySelector('.lf-title')?.textContent || '', cue: this.head?.querySelector('.lf-cue')?.textContent || '', math: this.a.math(),
      };
    },
  };
  window.LessonFlow = LF;
  const start = () => { if (!LF.init()) setTimeout(() => LF.init(), 300); };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else setTimeout(start, 0);
})();
