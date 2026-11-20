// 簡單折線圖（canvas）：多條線、十字游標顯示數值、可切換對數刻度
(function () {
  class LineChart {
    constructor(el, opts = {}) {
      this.el = el; this.opts = opts;
      this.cv = document.createElement('canvas');
      el.appendChild(this.cv);
      this.ctx = this.cv.getContext('2d');
      this.series = []; this.dates = []; this.mouse = null;
      this.cv.addEventListener('mousemove', e => {
        const r = this.cv.getBoundingClientRect();
        this.mouse = { x: e.clientX - r.left, y: e.clientY - r.top }; this.draw();
      });
      this.cv.addEventListener('mouseleave', () => { this.mouse = null; this.draw(); });
      if (window.ResizeObserver) new ResizeObserver(() => this.resize()).observe(el);
      this.resize();
    }
    set(dates, series, o = {}) { this.dates = dates; this.series = series; Object.assign(this.opts, o); this.draw(); }
    resize() {
      const r = this.el.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
      this.w = Math.max(200, r.width); this.h = Math.max(120, r.height);
      this.cv.width = this.w * dpr; this.cv.height = this.h * dpr;
      this.cv.style.width = this.w + 'px'; this.cv.style.height = this.h + 'px';
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0); this.draw();
    }
    draw() {
      if (this._raf) return;
      this._raf = requestAnimationFrame(() => { this._raf = null; this._draw(); });
    }
    _draw() {
      const ctx = this.ctx, W = this.w, H = this.h, n = this.dates.length;
      ctx.clearRect(0, 0, W, H);
      if (!n || !this.series.length) return;
      const s = getComputedStyle(this.el), g = k => s.getPropertyValue(k).trim();
      const L = 8, R = 78, T = 10, B = 22, pw = W - L - R, ph = H - T - B;
      const log = !!this.opts.log, fmt = this.opts.fmt || (v => v.toFixed(2));
      let mn = Infinity, mx = -Infinity;
      for (const se of this.series) for (const v of se.values) if (v != null && isFinite(v)) {
        if (log && v <= 0) continue;
        if (v < mn) mn = v; if (v > mx) mx = v;
      }
      if (!isFinite(mn)) return;
      if (this.opts.zero) { mn = Math.min(mn, 0); mx = Math.max(mx, 0); }
      const tf = v => log ? Math.log(v) : v;
      let a = tf(mn), b = tf(mx);
      if (a === b) { a -= 1; b += 1; }
      const pad = (b - a) * 0.05; a -= pad; b += pad;
      const y = v => T + (b - tf(v)) / (b - a) * ph, x = i => L + (n === 1 ? pw / 2 : i / (n - 1) * pw);
      ctx.font = '11px ui-monospace, Menlo, Consolas, monospace';
      ctx.fillStyle = g('--muted'); ctx.strokeStyle = g('--grid'); ctx.lineWidth = 1;
      // y 格線
      const ticks = [];
      if (log) {
        const lo = Math.exp(a), hi = Math.exp(b);
        const cands = [];
        for (let p = Math.floor(Math.log10(lo)); p <= Math.ceil(Math.log10(hi)); p++) for (const m of [1, 2, 5]) cands.push(m * Math.pow(10, p));
        for (const v of cands) if (v >= lo && v <= hi) ticks.push(v);
      } else {
        const raw = (b - a) / 5, p = Math.pow(10, Math.floor(Math.log10(raw))), m = raw / p;
        const st = (m < 1.5 ? 1 : m < 3 ? 2 : m < 7 ? 5 : 10) * p;
        for (let v = Math.ceil(a / st) * st; v <= b; v += st) ticks.push(v);
      }
      ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
      for (const v of ticks) {
        const yy = Math.round(y(v)) + 0.5;
        ctx.beginPath(); ctx.moveTo(L, yy); ctx.lineTo(L + pw, yy); ctx.stroke();
        ctx.fillText(fmt(v), L + pw + 6, yy);
      }
      if (this.opts.zero && !log) {
        ctx.strokeStyle = g('--muted'); const yy = Math.round(y(0)) + 0.5;
        ctx.beginPath(); ctx.moveTo(L, yy); ctx.lineTo(L + pw, yy); ctx.stroke(); ctx.strokeStyle = g('--grid');
      }
      // x：年份
      ctx.textAlign = 'center'; ctx.textBaseline = 'top';
      let lastX = -1e9;
      for (let i = 0; i < n; i++) {
        const d = this.dates[i];
        if (i === 0 || d.slice(0, 4) !== this.dates[i - 1].slice(0, 4)) {
          const xx = x(i);
          if (xx - lastX < 40) continue;
          ctx.beginPath(); ctx.moveTo(Math.round(xx) + 0.5, T); ctx.lineTo(Math.round(xx) + 0.5, T + ph); ctx.stroke();
          ctx.fillText(d.slice(0, 4), xx, T + ph + 5); lastX = xx;
        }
      }
      // 線
      ctx.save(); ctx.beginPath(); ctx.rect(L, T, pw, ph); ctx.clip();
      for (const se of this.series) {
        ctx.strokeStyle = se.color; ctx.lineWidth = se.width || 1.6; ctx.setLineDash(se.dash || []);
        ctx.beginPath(); let st = false;
        const step = Math.max(1, Math.floor(n / (pw * 2)));
        for (let i = 0; i < n; i += step) {
          const v = se.values[i];
          if (v == null || !isFinite(v) || (log && v <= 0)) { st = false; continue; }
          if (!st) { ctx.moveTo(x(i), y(v)); st = true; } else ctx.lineTo(x(i), y(v));
        }
        const lv = se.values[n - 1];
        if (st && lv != null && isFinite(lv)) ctx.lineTo(x(n - 1), y(lv));
        ctx.stroke();
      }
      ctx.setLineDash([]); ctx.restore();
      // 游標
      if (this.mouse && this.mouse.x >= L && this.mouse.x <= L + pw) {
        const i = Math.max(0, Math.min(n - 1, Math.round((this.mouse.x - L) / pw * (n - 1))));
        const xx = Math.round(x(i)) + 0.5;
        ctx.strokeStyle = g('--cross'); ctx.globalAlpha = 0.5; ctx.setLineDash([4, 4]);
        ctx.beginPath(); ctx.moveTo(xx, T); ctx.lineTo(xx, T + ph); ctx.stroke();
        ctx.setLineDash([]); ctx.globalAlpha = 1;
        const lines = [this.dates[i]].concat(this.series.map(se => `${se.name}：${se.values[i] == null ? '—' : fmt(se.values[i])}`));
        ctx.font = '12px -apple-system, "Segoe UI", "Microsoft JhengHei", sans-serif';
        const bw = Math.max(...lines.map(t => ctx.measureText(t).width)) + 16, bh = lines.length * 17 + 8;
        let bx = xx + 12; if (bx + bw > L + pw) bx = xx - 12 - bw;
        ctx.fillStyle = g('--panel2'); ctx.strokeStyle = g('--border');
        ctx.fillRect(bx, T + 6, bw, bh); ctx.strokeRect(bx + 0.5, T + 6.5, bw, bh);
        ctx.textAlign = 'left'; ctx.textBaseline = 'top';
        lines.forEach((t, k) => {
          ctx.fillStyle = k === 0 ? g('--text') : this.series[k - 1].color;
          ctx.fillText(t, bx + 8, T + 11 + k * 17);
        });
      }
    }
  }
  window.LineChart = LineChart;
})();
