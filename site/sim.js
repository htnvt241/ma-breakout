// 交易模擬：每日分批（學術做法）＋實戰模擬（N 個位子，多個起始日 × 隨機順序）＋ 0050 還原
(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const TAX = 0.003, FEE = 0.001425;
  const NAMES = { s1: '均線發散突破（首次訊號）', lh80: '長均線新高（80 日）', lh100: '長均線新高（100 日）' };
  let META, CAL, CODES = [], cache = {};

  // ---------- 格式
  const pct = (x, d = 1) => x == null || !isFinite(x) ? '—' : (x > 0 ? '+' : '') + (x * 100).toFixed(d) + '%';
  const cls = x => x == null || !isFinite(x) || Math.abs(x) < 5e-5 ? '' : x > 0 ? 'up' : 'down';
  const P = (x, d) => `<span class="${cls(x)}">${pct(x, d)}</span>`;
  const money = v => v == null ? '—' : Math.round(v).toLocaleString();
  const ymd = n => { const s = String(n); return `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}`; };

  // ---------- 亂數（可重現）
  function rng(seed) {
    let s = seed >>> 0 || 1;
    return () => { s ^= s << 13; s >>>= 0; s ^= s >> 17; s ^= s << 5; s >>>= 0; return s / 4294967296; };
  }

  // ---------- 載入
  async function getJSON(p) { const r = await fetch(p, { cache: 'no-cache' }); if (!r.ok) throw new Error(p + ' ' + r.status); return r.json(); }

  async function loadStrategy(key) {
    if (cache[key]) return cache[key];
    $('status').textContent = '下載模擬資料中…';
    const r = await fetch(`data/sim/${key}.bin`, { cache: 'no-cache' });
    if (!r.ok) throw new Error(key + ' ' + r.status);
    const buf = await r.arrayBuffer();
    const REC = META.rec, NH = META.horizons.length, PL = META.path_len;
    const n = Math.floor(buf.byteLength / REC), dv = new DataView(buf);
    const S = {
      n, sig: new Int32Array(n), ent: new Int32Array(n), stock: new Int32Array(n), flg: new Uint8Array(n),
      amt: new Float64Array(n), exOff: [], exR: [], path: new Uint16Array(buf), PL, base: 0,
    };
    for (let h = 0; h < NH; h++) { S.exOff.push([new Int32Array(n), new Int32Array(n)]); S.exR.push([new Float64Array(n), new Float64Array(n)]); }
    const pathStart = 10 + NH * 2 * 3;   // 位元組
    S.pathOff = new Int32Array(n);
    for (let i = 0; i < n; i++) {
      const o = i * REC;
      S.sig[i] = dv.getUint16(o, true);
      S.ent[i] = S.sig[i] + dv.getUint8(o + 2);
      S.stock[i] = dv.getUint16(o + 3, true);
      S.flg[i] = dv.getUint8(o + 5);
      S.amt[i] = dv.getUint32(o + 6, true);
      let p = o + 10;
      for (let h = 0; h < NH; h++) for (let m = 0; m < 2; m++) {
        const off = dv.getUint8(p), rr = dv.getUint16(p + 1, true); p += 3;
        S.exOff[h][m][i] = off === 255 ? -1 : off;
        S.exR[h][m][i] = rr / 1000;
      }
      S.pathOff[i] = (o + pathStart) / 2;
    }
    cache[key] = S;
    return S;
  }

  // 第 i 筆在交易日曆第 d 天收盤的價值（相對買進價）
  function val(S, i, d) {
    let k = d - S.ent[i];
    if (k < 0) return 1;
    if (k >= S.PL) k = S.PL - 1;
    const b = S.pathOff[i];
    for (; k >= 0; k--) { const v = S.path[b + k]; if (v) return v / 1000; }
    return 1;
  }

  // ---------- 模擬
  // P = {S, hi, mode(0/1), s, e, C, K, compound, fee, minAmt, limit, order}
  function prepare(P) {
    const S = P.S, byEnt = new Map();
    let total = 0, skipLimit = 0, skipAmt = 0;
    for (let i = 0; i < S.n; i++) {
      const d = S.ent[i];
      if (d < P.s || d > P.e) continue;
      total++;
      if (S.amt[i] < P.minAmt) { skipAmt++; continue; }
      if (P.limit && (S.flg[i] & 1)) { skipLimit++; continue; }
      if (!byEnt.has(d)) byEnt.set(d, []);
      byEnt.get(d).push(i);
    }
    for (const arr of byEnt.values()) arr.sort((a, b) => S.amt[b] - S.amt[a]);
    return { byEnt, total, skipLimit, skipAmt };
  }

  function exitDay(P, i) {
    const off = P.S.exOff[P.hi][P.mode][i];
    return off < 0 ? Infinity : P.S.ent[i] + off;
  }

  function sellValue(P, i, N, d, atExit) {
    const r = atExit ? P.S.exR[P.hi][P.mode][i] : val(P.S, i, d);
    return N * r * (1 - P.fee - TAX);
  }

  // 實戰模擬：K 個位子
  function runSlots(P, prep, startOff, seed, excl) {
    const S = P.S, rnd = seed ? rng(seed) : null;
    const s0 = P.s + startOff, len = P.e - s0 + 1;
    const eq = new Float64Array(len);
    let cash = P.C, pos = [], held = new Set(), trades = [], lastEq = P.C;
    for (let d = s0; d <= P.e; d++) {
      // 1. 開盤：先賣到期的
      if (pos.length) {
        const keep = [];
        for (const p of pos) {
          if (p.x === d) {
            const v = sellValue(P, p.i, p.N, d, true);
            cash += v; held.delete(S.stock[p.i]);
            trades.push({ i: p.i, ret: v / p.A - 1, A: p.A, pnl: v - p.A });
          } else keep.push(p);
        }
        pos = keep;
      }
      // 2. 開盤：買進
      let cands = prep.byEnt.get(d);
      if (cands && pos.length < P.K) {
        cands = cands.filter(i => !held.has(S.stock[i]) && !(excl && excl.has(i)));
        if (rnd) for (let k = cands.length - 1; k > 0; k--) { const j = Math.floor(rnd() * (k + 1)); [cands[k], cands[j]] = [cands[j], cands[k]]; }
        for (const i of cands) {
          if (pos.length >= P.K) break;
          const target = P.compound ? lastEq / P.K : P.C / P.K;
          const A = Math.min(target, cash);
          if (A < target * 0.5) break;     // 現金不到半份就不買
          cash -= A;
          pos.push({ i, A, N: A / (1 + P.fee), x: exitDay(P, i) });
          held.add(S.stock[i]);
        }
      }
      // 3. 收盤：計算資產
      let v = cash;
      for (const p of pos) v += p.N * val(S, p.i, d);
      eq[d - s0] = v; lastEq = v;
    }
    for (const p of pos) {   // 期末還沒賣的：用最後收盤估值（扣賣出成本）
      const v = sellValue(P, p.i, p.N, P.e, false);
      trades.push({ i: p.i, ret: v / p.A - 1, A: p.A, pnl: v - p.A, open: true });
    }
    return { s0, eq, trades };
  }

  // 每日分批：資金分成 H+1 份，每天輪到的那份平均買進當天所有訊號
  function runDaily(P, prep, excl) {
    const S = P.S, T = P.H + 1, len = P.e - P.s + 1;
    const base = P.C / T, cashT = new Float64Array(T).fill(base);
    const eq = new Float64Array(len);
    let pos = [], trades = [];
    for (let d = P.s; d <= P.e; d++) {
      if (pos.length) {
        const keep = [];
        for (const p of pos) {
          if (p.x === d) {
            const v = sellValue(P, p.i, p.N, d, true);
            cashT[p.t] += v;
            trades.push({ i: p.i, ret: v / p.A - 1, A: p.A, pnl: v - p.A });
          } else keep.push(p);
        }
        pos = keep;
      }
      let cands = prep.byEnt.get(d);
      if (cands && excl) cands = cands.filter(i => !excl.has(i));
      const t = (d - P.s) % T;
      if (cands && cands.length) {
        const total = P.compound ? cashT[t] : Math.min(cashT[t], base);
        if (total > 0) {
          const A = total / cands.length;
          cashT[t] -= total;
          for (const i of cands) pos.push({ i, t, A, N: A / (1 + P.fee), x: exitDay(P, i) });
        }
      }
      let v = 0;
      for (let k = 0; k < T; k++) v += cashT[k];
      for (const p of pos) v += p.N * val(S, p.i, d);
      eq[d - P.s] = v;
    }
    for (const p of pos) {
      const v = sellValue(P, p.i, p.N, P.e, false);
      trades.push({ i: p.i, ret: v / p.A - 1, A: p.A, pnl: v - p.A, open: true });
    }
    return { s0: P.s, eq, trades };
  }

  // ---------- 統計
  function years(s0, e) { return (new Date(ymd(CAL[e])) - new Date(ymd(CAL[s0]))) / (365.25 * 86400000); }
  function stats(run, C, e) {
    const eq = run.eq, fin = eq[eq.length - 1];
    let peak = -Infinity, mdd = 0;
    for (const v of eq) { if (v > peak) peak = v; const dd = v / peak - 1; if (dd < mdd) mdd = dd; }
    const yr = years(run.s0, e);
    const out = { fin, tot: fin / C - 1, cagr: yr > 0 ? Math.pow(fin / C, 1 / yr) - 1 : null, mdd };
    if (run.trades) {
      const t = run.trades, w = t.filter(x => x.ret > 0), l = t.filter(x => x.ret <= 0);
      const avg = a => a.length ? a.reduce((s, x) => s + x.ret, 0) / a.length : null;
      out.n = t.length; out.win = t.length ? w.length / t.length : null; out.avg = avg(t);
      const aw = avg(w), al = avg(l);
      out.pl = aw != null && al ? aw / Math.abs(al) : null;
    }
    return out;
  }
  function benchRun(P) {
    const len = P.e - P.s + 1, eq = new Float64Array(len), b0 = META.bench[P.s];
    for (let d = P.s; d <= P.e; d++) eq[d - P.s] = P.C * META.bench[d] / b0;
    return { s0: P.s, eq };
  }
  // 某段期間（日曆索引 a~b）的報酬：用資產曲線
  function seg(run, a, b) {
    const ia = a - run.s0, ib = b - run.s0;
    if (ia < 0 || ib >= run.eq.length || ib <= ia) return null;
    return run.eq[ib] / run.eq[ia] - 1;
  }

  // ---------- 主流程
  async function simulate() {
    const key = $('strategy').value;
    let S;
    try { S = await loadStrategy(key); } catch (e) { $('status').textContent = '模擬資料載入失敗：' + e.message; return; }
    const sDate = $('dFrom').value.replace(/-/g, ''), eDate = $('dTo').value.replace(/-/g, '');
    let s = CAL.findIndex(d => d >= +sDate), e = -1;
    for (let k = CAL.length - 1; k >= 0; k--) if (CAL[k] <= +eDate) { e = k; break; }
    if (s < 0 || e <= s + 30) { $('status').textContent = '日期區間太短，請至少選 30 個交易日以上。'; return; }
    const hi = +$('hold').value, H = META.horizons[hi];
    const P = {
      S, hi, H, mode: $('limit').checked ? 1 : 0, s, e,
      C: Math.max(10000, +$('capital').value || 1e6), K: Math.max(1, Math.min(100, +$('slots').value || 10)),
      compound: $('money').value === 'compound', fee: FEE * (+$('fee').value),
      minAmt: +$('minAmt').value, limit: $('limit').checked, order: $('order').value,
    };
    $('status').textContent = '計算中…';
    await new Promise(r => setTimeout(r, 20));
    const t0 = performance.now();
    const prep = prepare(P);

    const daily = runDaily(P, prep);
    const maxOff = Math.min(20, Math.max(1, Math.floor((e - s) / 10)));
    const seeds = P.order === 'random' ? 5 : 1;
    const runs = [];
    for (let k = 0; k < maxOff; k++) for (let j = 0; j < seeds; j++) {
      const r = runSlots(P, prep, k, P.order === 'random' ? 1 + k * 7919 + j * 104729 : 0);
      r.st = stats(r, P.C, e); runs.push(r);
    }
    runs.sort((a, b) => a.st.cagr - b.st.cagr);
    const worst = runs[0], best = runs[runs.length - 1], med = runs[Math.floor((runs.length - 1) / 2)];
    const bench = benchRun(P);
    const stD = stats(daily, P.C, e), stB = stats(bench, P.C, e);

    // 拿掉最賺的交易：依單筆報酬排序（同一組符合條件的訊號）
    const elig = [];
    for (const arr of prep.byEnt.values()) for (const i of arr) {
      const x = exitDay(P, i);
      const r = (x <= e ? S.exR[hi][P.mode][i] : val(S, i, e)) * (1 - P.fee - TAX) / (1 + P.fee) - 1;
      elig.push([i, r]);
    }
    elig.sort((a, b) => b[1] - a[1]);
    const rm = [0.01, 0.05].map(q => {
      const k = Math.max(1, Math.round(elig.length * q)), ex = new Set(elig.slice(0, k).map(x => x[0]));
      const d2 = stats(runDaily(P, prep, ex), P.C, e);
      const rr = [];
      for (let o = 0; o < maxOff; o++) for (let j = 0; j < seeds; j++) rr.push(stats(runSlots(P, prep, o, P.order === 'random' ? 1 + o * 7919 + j * 104729 : 0, ex), P.C, e).cagr);
      rr.sort((a, b) => a - b);
      return { q, k, daily: d2.cagr, slots: rr[Math.floor((rr.length - 1) / 2)] };
    });

    render(P, prep, { daily, stD, runs, worst, best, med, bench, stB, rm, elig });
    $('status').textContent = `完成（${runs.length} 次實戰模擬，${((performance.now() - t0) / 1000).toFixed(1)} 秒）`;
  }

  // ---------- 畫面
  let eqChart, ddChart, last;
  function render(Q, prep, R) {
    last = { P: Q, R };
    const nEl = R.elig.length;
    $('summaryNote').innerHTML =
      `${NAMES[$('strategy').value]}，${ymd(CAL[Q.s])} ～ ${ymd(CAL[Q.e])}（${years(Q.s, Q.e).toFixed(1)} 年），持有 ${Q.H} 個交易日。` +
      `期間內訊號 ${prep.total.toLocaleString()} 筆，排除成交金額不足 ${prep.skipAmt.toLocaleString()} 筆` +
      (Q.limit ? `、開盤漲停買不到 ${prep.skipLimit.toLocaleString()} 筆` : '') + `，可交易 ${nEl.toLocaleString()} 筆。`;

    const row = (name, st, extra = '') => `<tr><td class="l">${name}</td><td>${money(st.fin)}</td><td>${P(st.tot)}</td><td><b>${P(st.cagr, 2)}</b></td>` +
      `<td>${P(st.mdd)}</td><td>${st.n != null ? st.n.toLocaleString() : '—'}</td><td>${st.win != null ? (st.win * 100).toFixed(1) + '%' : '—'}</td>` +
      `<td>${st.avg != null ? P(st.avg, 2) : '—'}</td><td>${st.pl != null ? st.pl.toFixed(2) : '—'}</td><td class="l muted">${extra}</td></tr>`;
    const k = R.runs.length;
    $('summary').innerHTML = `<table class="res"><tr><th class="l">方法</th><th>期末資產</th><th>總報酬</th><th>年化報酬</th><th>最大回撤</th><th>交易筆數</th><th>勝率</th><th>平均每筆</th><th>賺賠比</th><th class="l">說明</th></tr>` +
      row('每日分批', R.stD, '每個訊號都買到，沒有運氣成分') +
      row('實戰模擬：最差', R.worst.st, `${k} 次模擬中年化最低（從 ${ymd(CAL[R.worst.s0])} 開始）`) +
      row('實戰模擬：中位數', R.med.st, `${k} 次模擬的中間值`) +
      row('實戰模擬：最好', R.best.st, `${k} 次模擬中年化最高（從 ${ymd(CAL[R.best.s0])} 開始）`) +
      row('0050 買進持有（還原）', R.stB, `期初 ${ymd(CAL[Q.s])} 收盤全部買進 0050，一路持有到期末，不看任何訊號；股利再投入，未扣手續費`) + '</table>';

    // 資產曲線
    const dates = [];
    for (let d = Q.s; d <= Q.e; d++) dates.push(ymd(CAL[d]));
    const align = run => { const a = new Array(dates.length).fill(null); for (let j = 0; j < run.eq.length; j++) a[run.s0 - Q.s + j] = run.eq[j] / 1e4; return a; };
    const css = getComputedStyle(document.documentElement);
    const c = { d: css.getPropertyValue('--accent').trim(), m: css.getPropertyValue('--signal').trim(), b: css.getPropertyValue('--muted').trim(), w: css.getPropertyValue('--down').trim(), g: css.getPropertyValue('--up').trim() };
    const eqS = [
      { name: '每日分批', color: c.d, values: align(R.daily) },
      { name: '實戰中位數', color: c.m, values: align(R.med) },
      { name: '實戰最好', color: c.g, values: align(R.best), width: 1, dash: [4, 3] },
      { name: '實戰最差', color: c.w, values: align(R.worst), width: 1, dash: [4, 3] },
      { name: '0050 還原', color: c.b, values: align(R.bench), width: 2 },
    ];
    eqChart.set(dates, eqS, { log: $('logScale').checked, fmt: v => v >= 1000 ? Math.round(v).toLocaleString() + ' 萬' : v.toFixed(v < 10 ? 1 : 0) + ' 萬' });
    const dd = vals => { let pk = -Infinity; return vals.map(v => { if (v == null) return null; if (v > pk) pk = v; return v / pk - 1; }); };
    ddChart.set(dates, eqS.filter((_, j) => j !== 2 && j !== 3).map(se => ({ ...se, values: dd(se.values) })),
      { fmt: v => (v * 100).toFixed(0) + '%', zero: true });

    // 逐年
    const yearsList = [];
    for (let d = Q.s; d <= Q.e; d++) { const y = String(CAL[d]).slice(0, 4); if (!yearsList.length || yearsList[yearsList.length - 1].y !== y) yearsList.push({ y, a: d, b: d }); else yearsList[yearsList.length - 1].b = d; }
    const yr = yearsList.map(o => {
      const a = Math.max(Q.s, o.a - 1), b = o.b;
      const v = [seg(R.daily, a, b), seg(R.med, Math.max(a, R.med.s0), b), seg(R.bench, a, b)];
      const partS = o.a === Q.s && Q.s > 0 && String(CAL[Q.s - 1]).slice(0, 4) === o.y;
      const partE = o.b === Q.e && (Q.e === CAL.length - 1 || String(CAL[Q.e + 1]).slice(0, 4) === o.y);
      return `<tr><td class="l">${o.y}${partS || partE ? '<span class="muted">（部分）</span>' : ''}</td>${v.map(x => `<td>${P(x)}</td>`).join('')}<td>${v[0] != null && v[2] != null ? P(v[0] - v[2]) : '—'}</td></tr>`;
    }).join('');
    $('yearly').innerHTML = `<table class="res"><tr><th class="l">年度</th><th>每日分批</th><th>實戰中位數</th><th>0050 還原</th><th>每日分批 − 0050</th></tr>${yr}</table>`;

    // 前後兩半
    const mid = Math.floor((Q.s + Q.e) / 2);
    const half = (a, b) => {
      const yrs = years(a, b), f = x => x == null ? null : Math.pow(1 + x, 1 / yrs) - 1;
      return [f(seg(R.daily, a, b)), f(seg(R.med, Math.max(a, R.med.s0), b)), f(seg(R.bench, a, b))];
    };
    const h1 = half(Q.s, mid), h2 = half(mid, Q.e);
    $('halves').innerHTML = `<table class="res"><tr><th class="l">期間（年化報酬）</th><th>每日分批</th><th>實戰中位數</th><th>0050 還原</th></tr>` +
      `<tr><td class="l">前半段 ${ymd(CAL[Q.s])} ～ ${ymd(CAL[mid])}</td>${h1.map(x => `<td>${P(x, 2)}</td>`).join('')}</tr>` +
      `<tr><td class="l">後半段 ${ymd(CAL[mid])} ～ ${ymd(CAL[Q.e])}</td>${h2.map(x => `<td>${P(x, 2)}</td>`).join('')}</tr></table>`;

    // 拿掉最賺的交易
    $('removeTop').innerHTML = `<table class="res"><tr><th class="l">年化報酬</th><th>每日分批</th><th>實戰中位數</th><th>0050 還原</th></tr>` +
      `<tr><td class="l">全部交易</td><td>${P(R.stD.cagr, 2)}</td><td>${P(R.med.st.cagr, 2)}</td><td>${P(R.stB.cagr, 2)}</td></tr>` +
      R.rm.map(x => `<tr><td class="l">拿掉最賺的 ${x.q * 100}%（${x.k.toLocaleString()} 筆）</td><td>${P(x.daily, 2)}</td><td>${P(x.slots, 2)}</td><td>${P(R.stB.cagr, 2)}</td></tr>`).join('') + '</table>';

    // 實戰模擬的分布
    const cg = R.runs.map(r => r.st.cagr), q = p => cg[Math.min(cg.length - 1, Math.max(0, Math.round((cg.length - 1) * p)))];
    $('dist').innerHTML = `${R.runs.length} 次實戰模擬（${Math.min(20, R.runs.length / (Q.order === 'random' ? 5 : 1))} 個不同起始日` +
      (Q.order === 'random' ? ' × 5 種隨機買進順序' : '，同一天依成交金額大到小買進') + `）的年化報酬：` +
      `最差 ${P(q(0), 2)}、前 10% ${P(q(0.1), 2)}、中位數 ${P(q(0.5), 2)}、後 10% ${P(q(0.9), 2)}、最好 ${P(q(1), 2)}。` +
      `0050 同期 ${P(R.stB.cagr, 2)}；<b>${cg.filter(x => x > R.stB.cagr).length} / ${cg.length}</b> 次跑贏 0050。`;
    $('results').classList.remove('hidden');
  }

  // ---------- 初始化
  async function init() {
    try { META = await getJSON('data/sim/meta.json'); } catch (e) { $('status').textContent = '模擬資料載入失敗：' + e.message; return; }
    CAL = META.cal;
    const first = CAL.findIndex(d => d >= +META.sim_start.replace(/-/g, ''));
    $('dFrom').min = $('dTo').min = ymd(CAL[first]);
    $('dFrom').max = $('dTo').max = ymd(CAL[CAL.length - 1]);
    $('dFrom').value = ymd(CAL[first]);
    $('dTo').value = ymd(CAL[CAL.length - 1]);
    $('hold').innerHTML = META.horizons.map((h, i) => `<option value="${i}" ${h === 20 ? 'selected' : ''}>${h} 個交易日</option>`).join('');
    for (const k of Object.keys(META.strategies)) {
      const o = $('strategy').querySelector(`option[value="${k}"]`);
      if (o) o.textContent = `${NAMES[k]}（${META.strategies[k].count.toLocaleString()} 筆訊號）`;
    }
    eqChart = new LineChart($('eqChart'));
    ddChart = new LineChart($('ddChart'));
    const upd = () => { $('perSlot').textContent = `每檔約 ${Math.round((+$('capital').value || 0) / Math.max(1, +$('slots').value || 1)).toLocaleString()} 元`; };
    $('capital').addEventListener('input', upd); $('slots').addEventListener('input', upd); upd();
    $('run').onclick = simulate;
    $('logScale').onchange = () => { if (last) render(last.P, prepare(last.P), last.R); };
    $('theme').onclick = () => {
      const light = document.documentElement.dataset.theme !== 'light';
      if (light) document.documentElement.dataset.theme = 'light'; else delete document.documentElement.dataset.theme;
      try { localStorage.setItem('theme', light ? 'light' : 'dark'); } catch (e) {}
      $('themeLabel').textContent = light ? '日間' : '夜間';
      if (last) render(last.P, prepare(last.P), last.R);
    };
    if (document.documentElement.dataset.theme === 'light') $('themeLabel').textContent = '日間';
    $('status').textContent = '設定好條件後，按「開始模擬」。';
  }

  // 給測試用
  window.__sim = { loadStrategy, prepare, runDaily, runSlots, stats, val, get META() { return META; } };
  init();
})();
