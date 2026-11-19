// K 線圖（自製 canvas，不依賴外部套件）
// 功能：K 棒、5 條均線、成交量、訊號標記、訊號收盤虛線、十字游標、滾輪縮放、拖曳平移、雙擊還原
(function () {
  const AXIS_W = 64, AXIS_H = 24, VOL_RATIO = 0.2, GAP = 8;

  function niceStep(range, count) {
    const raw = range / count, p = Math.pow(10, Math.floor(Math.log10(raw)));
    const m = raw / p;
    return (m < 1.5 ? 1 : m < 3 ? 2 : m < 7 ? 5 : 10) * p;
  }

  class KChart {
    constructor(el, opts = {}) {
      this.el = el;
      this.opts = opts;
      this.cv = document.createElement('canvas');
      this.cv.className = 'kchart-canvas';
      el.appendChild(this.cv);
      this.ctx = this.cv.getContext('2d');
      this.bars = [];
      this.mouse = null;
      this._bind();
      if (window.ResizeObserver) new ResizeObserver(() => this.resize()).observe(el);
      this.resize();
    }

    setData(d) {
      // d: {bars:[{t,o,h,l,c,v}], ma:{5:[],...}, maColors:{}, sigIdx, otherIdx:[], priceLine, from}
      Object.assign(this, { bars: d.bars, ma: d.ma, maColors: d.maColors, sigIdx: d.sigIdx,
        otherIdx: new Set(d.otherIdx || []), priceLine: d.priceLine, from: d.from || 0 });
      this.resetView();
    }

    resetView() {
      const n = this.bars.length;
      if (!n) return;
      const pw = this.plotW();
      const count = Math.max(20, n - this.from + 3);
      this.spacing = Math.min(24, Math.max(4, pw / count));
      // 放得下就畫到最新一天；放不下（很久以前的訊號）就從訊號前 60 天開始往右畫，可拖曳看後面
      this.right = count * this.spacing <= pw ? n - 1 + 3 : this.from + pw / this.spacing - 1;
      this.draw();
    }

    plotW() { return Math.max(50, this.w - AXIS_W); }

    resize() {
      const r = this.el.getBoundingClientRect();
      this.w = Math.max(200, r.width);
      this.h = Math.max(200, r.height);
      const dpr = window.devicePixelRatio || 1;
      this.cv.width = Math.round(this.w * dpr);
      this.cv.height = Math.round(this.h * dpr);
      this.cv.style.width = this.w + 'px';
      this.cv.style.height = this.h + 'px';
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      this.draw();
    }

    x(i) { return this.plotW() - (this.right - i) * this.spacing - this.spacing / 2; }

    idxAt(px) { return Math.round(this.right - (this.plotW() - px - this.spacing / 2) / this.spacing); }

    clampView() {
      const n = this.bars.length, vis = this.plotW() / this.spacing;
      this.right = Math.min(n - 1 + vis * 0.7, Math.max(Math.min(n - 1, vis * 0.3), this.right));
    }

    _bind() {
      const cv = this.cv;
      let drag = null, pinch = null;
      cv.addEventListener('mousemove', e => {
        const r = cv.getBoundingClientRect();
        const mx = e.clientX - r.left, my = e.clientY - r.top;
        if (drag) {
          this.right = drag.right - (mx - drag.x) / this.spacing;
          this.clampView();
        }
        this.mouse = { x: mx, y: my };
        this.draw();
      });
      cv.addEventListener('mouseleave', () => { this.mouse = null; drag = null; this.draw(); });
      cv.addEventListener('mousedown', e => {
        const r = cv.getBoundingClientRect();
        drag = { x: e.clientX - r.left, right: this.right };
        cv.style.cursor = 'grabbing';
      });
      window.addEventListener('mouseup', () => { drag = null; cv.style.cursor = ''; });
      cv.addEventListener('dblclick', () => this.resetView());
      cv.addEventListener('wheel', e => {
        if (!this.bars.length) return;
        e.preventDefault();
        const r = cv.getBoundingClientRect();
        const mx = Math.min(this.plotW(), e.clientX - r.left);
        const anchor = this.right - (this.plotW() - mx - this.spacing / 2) / this.spacing;
        const f = e.deltaY < 0 ? 1.12 : 1 / 1.12;
        this.spacing = Math.min(40, Math.max(1, this.spacing * f));
        this.right = anchor + (this.plotW() - mx - this.spacing / 2) / this.spacing;
        this.clampView();
        this.draw();
      }, { passive: false });
      cv.addEventListener('touchstart', e => {
        if (e.touches.length === 1) drag = { x: e.touches[0].clientX, right: this.right };
        if (e.touches.length === 2) {
          pinch = { d: Math.abs(e.touches[0].clientX - e.touches[1].clientX), s: this.spacing };
          drag = null;
        }
      }, { passive: true });
      cv.addEventListener('touchmove', e => {
        if (drag && e.touches.length === 1) {
          e.preventDefault();
          this.right = drag.right - (e.touches[0].clientX - drag.x) / this.spacing;
        } else if (pinch && e.touches.length === 2) {
          e.preventDefault();
          const d = Math.abs(e.touches[0].clientX - e.touches[1].clientX);
          this.spacing = Math.min(40, Math.max(1, pinch.s * d / Math.max(10, pinch.d)));
        }
        this.clampView();
        this.draw();
      }, { passive: false });
      cv.addEventListener('touchend', () => { drag = null; pinch = null; });
    }

    colors() {
      const s = getComputedStyle(this.el);
      const g = k => s.getPropertyValue(k).trim();
      return { up: g('--up'), down: g('--down'), grid: g('--grid'), text: g('--muted'),
        bg: g('--panel'), cross: g('--cross'), accent: g('--accent'), signal: g('--signal') };
    }

    draw() {
      if (this._raf) return;
      this._raf = requestAnimationFrame(() => { this._raf = null; this._draw(); });
    }

    _draw() {
      const ctx = this.ctx, W = this.w, H = this.h, n = this.bars.length;
      ctx.clearRect(0, 0, W, H);
      if (!n) return;
      const C = this.colors(), pw = this.plotW();
      const priceH = (H - AXIS_H) * (1 - VOL_RATIO) - GAP, volTop = priceH + GAP, volH = H - AXIS_H - volTop;
      const lo = Math.max(0, Math.ceil(this.right - pw / this.spacing)), hi = Math.min(n - 1, Math.floor(this.right));
      if (hi < lo) return;

      let pmin = Infinity, pmax = -Infinity, vmax = 0;
      for (let i = lo; i <= hi; i++) {
        const b = this.bars[i];
        if (b.l < pmin) pmin = b.l;
        if (b.h > pmax) pmax = b.h;
        if (b.v > vmax) vmax = b.v;
        for (const k of [5, 10, 20, 60, 120]) {
          const m = this.ma[k][i];
          if (m != null) { if (m < pmin) pmin = m; if (m > pmax) pmax = m; }
        }
      }
      if (this.priceLine && this.sigIdx >= lo && this.sigIdx <= hi) {
        pmin = Math.min(pmin, this.priceLine); pmax = Math.max(pmax, this.priceLine);
      }
      const pad = (pmax - pmin) * 0.08 || pmax * 0.02 || 1;
      pmin -= pad; pmax += pad * 1.6;   // 下方多留一點給訊號箭頭
      pmin -= pad * 0.6;
      const y = p => 8 + (pmax - p) / (pmax - pmin) * (priceH - 8);
      const yv = v => volTop + volH - (vmax ? v / vmax * volH : 0);
      this._y = y; this._pmin = pmin; this._pmax = pmax; this._priceH = priceH;

      ctx.font = '11px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
      ctx.lineWidth = 1;

      // 價格格線
      const step = niceStep(pmax - pmin, 6);
      ctx.fillStyle = C.text; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
      const dec = Math.max(0, -Math.floor(Math.log10(step) + 1e-9));
      for (let p = Math.ceil(pmin / step) * step; p <= pmax; p += step) {
        const yy = Math.round(y(p)) + 0.5;
        if (yy < 6 || yy > priceH) continue;
        ctx.strokeStyle = C.grid; ctx.beginPath(); ctx.moveTo(0, yy); ctx.lineTo(pw, yy); ctx.stroke();
        ctx.fillText(p.toFixed(dec), pw + 8, yy);
      }

      // 日期軸
      ctx.textAlign = 'center'; ctx.textBaseline = 'top';
      const minGap = 54, every = Math.max(1, Math.ceil(minGap / this.spacing));
      let lastX = -1e9, lastI = -1e9;
      for (let i = lo; i <= hi; i++) {
        const t = this.bars[i].t, prev = i > 0 ? this.bars[i - 1].t : null;
        const monthStart = !prev || prev.slice(0, 7) !== t.slice(0, 7);
        const xx = this.x(i);
        let label = null;
        if (monthStart && xx - lastX >= minGap * 0.8) {
          const m = +t.slice(5, 7);
          label = m === 1 ? t.slice(0, 4) : m + '月';
        } else if (!monthStart && i - lastI >= every && xx - lastX >= minGap) {
          // 下個月初很近就先不標，讓月份標籤有位置
          let nearMonth = false;
          for (let j = i + 1; j <= Math.min(hi, i + Math.ceil(every / 2)); j++) {
            if (this.bars[j].t.slice(0, 7) !== t.slice(0, 7)) { nearMonth = true; break; }
          }
          if (!nearMonth) label = (+t.slice(8, 10)) + '日';
        }
        if (label && xx < 18) label = null;
        if (label) {
          ctx.strokeStyle = C.grid; ctx.beginPath();
          ctx.moveTo(Math.round(xx) + 0.5, 0); ctx.lineTo(Math.round(xx) + 0.5, H - AXIS_H); ctx.stroke();
          ctx.fillStyle = monthStart ? C.cross : C.text;
          ctx.font = (monthStart ? 'bold ' : '') + '11px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
          ctx.fillText(label, xx, H - AXIS_H + 6);
          lastX = xx; lastI = i;
        }
      }
      ctx.font = '11px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';

      ctx.save();
      ctx.beginPath(); ctx.rect(0, 0, pw, H - AXIS_H); ctx.clip();

      // 成交量
      const bw = Math.max(1, this.spacing * 0.7);
      for (let i = lo; i <= hi; i++) {
        const b = this.bars[i], up = b.c >= b.o;
        ctx.fillStyle = up ? C.up : C.down;
        ctx.globalAlpha = 0.45;
        const yy = yv(b.v);
        ctx.fillRect(this.x(i) - bw / 2, yy, bw, volTop + volH - yy);
      }
      ctx.globalAlpha = 1;

      // K 棒
      for (let i = lo; i <= hi; i++) {
        const b = this.bars[i], up = b.c >= b.o, xx = Math.round(this.x(i)) + 0.5;
        ctx.strokeStyle = ctx.fillStyle = up ? C.up : C.down;
        ctx.beginPath(); ctx.moveTo(xx, y(b.h)); ctx.lineTo(xx, y(b.l)); ctx.stroke();
        const top = y(Math.max(b.o, b.c)), bot = y(Math.min(b.o, b.c));
        ctx.fillRect(xx - bw / 2, top, bw, Math.max(1, bot - top));
      }

      // 均線
      for (const k of [120, 60, 20, 10, 5]) {
        const arr = this.ma[k];
        ctx.strokeStyle = this.maColors[k]; ctx.lineWidth = 1.3; ctx.beginPath();
        let started = false;
        for (let i = Math.max(0, lo - 1); i <= Math.min(n - 1, hi + 1); i++) {
          if (arr[i] == null) { started = false; continue; }
          const xx = this.x(i), yy = y(arr[i]);
          if (!started) { ctx.moveTo(xx, yy); started = true; } else ctx.lineTo(xx, yy);
        }
        ctx.stroke();
      }
      ctx.lineWidth = 1;

      // 訊號收盤虛線
      if (this.priceLine) {
        const yy = Math.round(y(this.priceLine)) + 0.5;
        if (yy > 0 && yy < priceH) {
        ctx.strokeStyle = C.text; ctx.setLineDash([2, 3]);
        ctx.beginPath(); ctx.moveTo(0, yy); ctx.lineTo(pw, yy); ctx.stroke();
        ctx.setLineDash([]);
        }
      }

      // 其他訊號日（亮黃色小三角）
      ctx.fillStyle = C.signal;
      for (const i of this.otherIdx) {
        if (i < lo || i > hi || i === this.sigIdx) continue;
        const xx = this.x(i), yy = y(this.bars[i].l) + 6;
        ctx.beginPath(); ctx.moveTo(xx, yy); ctx.lineTo(xx - 4, yy + 7); ctx.lineTo(xx + 4, yy + 7); ctx.fill();
      }
      // 選取的訊號日（亮黃色箭頭）
      if (this.sigIdx != null && this.sigIdx >= lo && this.sigIdx <= hi) {
        const xx = this.x(this.sigIdx), yy = y(this.bars[this.sigIdx].l) + 6;
        ctx.fillStyle = C.signal;
        ctx.beginPath(); ctx.moveTo(xx, yy); ctx.lineTo(xx - 7, yy + 10); ctx.lineTo(xx - 2.5, yy + 10);
        ctx.lineTo(xx - 2.5, yy + 18); ctx.lineTo(xx + 2.5, yy + 18); ctx.lineTo(xx + 2.5, yy + 10);
        ctx.lineTo(xx + 7, yy + 10); ctx.closePath(); ctx.fill();
        ctx.textAlign = 'center'; ctx.textBaseline = 'top';
        ctx.fillText('訊號', xx, yy + 21);
      }

      // 十字游標
      let hover = null;
      if (this.mouse && this.mouse.x < pw && this.mouse.y < H - AXIS_H) {
        hover = Math.min(hi, Math.max(lo, this.idxAt(this.mouse.x)));
        const xx = Math.round(this.x(hover)) + 0.5, yy = Math.round(this.mouse.y) + 0.5;
        ctx.strokeStyle = C.cross; ctx.setLineDash([4, 4]); ctx.globalAlpha = 0.6;
        ctx.beginPath(); ctx.moveTo(xx, 0); ctx.lineTo(xx, H - AXIS_H); ctx.moveTo(0, yy); ctx.lineTo(pw, yy); ctx.stroke();
        ctx.setLineDash([]); ctx.globalAlpha = 1;
      }
      ctx.restore();

      // 右側價格標籤
      const tag = (p, bg, fg) => {
        const yy = y(p);
        if (yy < 0 || yy > priceH) return;
        ctx.fillStyle = bg; ctx.fillRect(pw + 1, yy - 9, AXIS_W - 2, 18);
        ctx.fillStyle = fg; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
        ctx.fillText(p.toFixed(2), pw + 8, yy);
      };
      const last = this.bars[n - 1];
      if (hi === n - 1) tag(last.c, last.c >= last.o ? C.up : C.down, '#fff');
      if (this.priceLine) tag(this.priceLine, C.accent, '#fff');
      if (hover != null) {
        const p = pmax - (this.mouse.y - 8) / (priceH - 8) * (pmax - pmin);
        if (this.mouse.y <= priceH) tag(p, C.cross, C.bg);
        const xx = this.x(hover), t = this.bars[hover].t;
        ctx.fillStyle = C.cross; ctx.fillRect(xx - 40, H - AXIS_H + 2, 80, 18);
        ctx.fillStyle = C.bg; ctx.textAlign = 'center'; ctx.textBaseline = 'top';
        ctx.fillText(t, xx, H - AXIS_H + 5);
      }
      if (this.opts.onHover && hover !== this._lastHover) {
        this._lastHover = hover;
        this.opts.onHover(hover);
      }
    }
  }

  window.KChart = KChart;
})();
