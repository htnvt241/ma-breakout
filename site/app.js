// 均線發散突破：網頁主程式
(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const PAGE = 60;
  const TYPE = ['首次', '再次', '連續'];
  const MA_COLORS = () => {
    const s = getComputedStyle(document.documentElement);
    const o = {};
    for (const k of [5, 10, 20, 60, 120]) o[k] = s.getPropertyValue('--ma' + k).trim();
    return o;
  };
  const collator = new Intl.Collator('zh-Hant-TW-u-co-stroke');

  let META, STOCKS, SIGS, BENCH = null;
  let view = [];          // 篩選＋排序後的訊號
  let sel = 0, page = 0, tableH = 0;
  let sortKey = 'd', sortDir = -1;
  const pxCache = new Map();
  let chart, curPx = null, curSig = null;

  // ---------- 格式
  const pct = (x, d = 1) => {
    if (x == null) return '—';
    const v = (x * 100).toFixed(d);
    return +v === 0 ? (0).toFixed(d) + '%' : (x > 0 ? '+' : '') + v + '%';
  };
  const cls = (x, d = 1) => x == null || +(x * 100).toFixed(d) === 0 ? '' : x > 0 ? 'up' : 'down';
  const pctSpan = (x, d = 1) => `<span class="${cls(x, d)}">${pct(x, d)}</span>`;
  const amtFmt = wan => wan >= 1e4 ? (wan / 1e4).toFixed(1) + ' 億' : Math.round(wan).toLocaleString() + ' 萬';
  const num = (x, d = 2) => x == null ? '—' : (+x).toFixed(d);
  const dShort = d => d.replace(/-/g, '/');

  // ---------- 表格欄位
  const COLS = [
    { k: 'd', t: '訊號日', l: 1, f: s => dShort(s.d) },
    { k: 'code', t: '代號', l: 1, f: s => `<b>${s.code}</b>` },
    { k: 'name', t: '公司', l: 1, cls: 'name', f: s => s.name, cmp: (a, b) => collator.compare(a.name, b.name) },
    { k: 'ind', t: '產業', l: 1, cls: 'ind', f: s => s.ind, cmp: (a, b) => collator.compare(a.ind, b.ind) },
    { k: 't', t: '類型', l: 1, f: badge, val: s => s.t * 100 + s.k },
    { k: 'c', t: '收盤', f: s => String(+s.c.toFixed(2)) },
    { k: 'chg', t: '當日漲幅', f: s => pctSpan(s.chg) },
    { k: 'amt', t: '成交額', f: s => amtFmt(s.amt) },
    { k: 'm5', t: '5MA 5日', f: s => pct(s.m5, 2) },
    { k: 'm10', t: '10MA 5日', f: s => pct(s.m10, 2) },
    { k: 'm20', t: '20MA 5日', f: s => pct(s.m20, 2) },
    { k: 'r5', t: '+5日', f: s => pctSpan(s.r5) },
    { k: 'r10', t: '+10日', f: s => pctSpan(s.r10) },
    { k: 'r20', t: '+20日', f: s => pctSpan(s.r20) },
    { k: 'rn', t: '至今報酬', f: s => pctSpan(s.rn) },
    { k: 'hi', t: '至今最高', f: s => pctSpan(s.hi) },
    { k: 'lo', t: '至今最低', f: s => pctSpan(s.lo) },
  ];
  function badge(s) {
    if (s.t === 2) return `<span class="badge cont">連續 ${s.k} 天</span>`;
    return `<span class="badge ${s.t === 1 ? 'again' : ''}">${TYPE[s.t]}</span>`;
  }

  // ---------- 載入
  async function getJSON(p) {
    const r = await fetch(p, { cache: 'no-cache' });
    if (!r.ok) throw new Error(p + ' ' + r.status);
    return r.json();
  }

  async function init() {
    try {
      [META, STOCKS, SIGS] = await Promise.all(['data/meta.json', 'data/stocks.json', 'data/signals.json'].map(getJSON));
    } catch (e) {
      $('lastDate').textContent = '資料載入失敗';
      $('metaText').textContent = String(e);
      return;
    }
    const F = SIGS.fields, list = STOCKS.list;
    SIGS = SIGS.rows.map(r => {
      const o = {};
      F.forEach((f, i) => { o[f] = r[i]; });
      const st = list[o.s];
      o.code = st[0]; o.name = st[1]; o.market = st[2]; o.ind = st[3];
      return o;
    });
    const bd = META.bench;
    BENCH = new Map(bd.d.map((d, i) => [ymd(d), bd.c[i]]));

    $('lastDate').textContent = '資料收盤日 ' + META.last_date;
    $('metaText').textContent =
      `${META.sig_start} 起共 ${META.signals.toLocaleString()} 筆訊號（首次 ${META.first.toLocaleString()} 筆）、` +
      `${META.stocks_with_signal.toLocaleString()} 檔個股；股票池 ${META.pool.toLocaleString()} 檔。` +
      `最新一日 ${META.latest_all} 檔符合（首次 ${META.latest_first} 檔）。頁面產生 ${META.generated}（台北時間）。`;

    $('fFrom').value = $('fFrom').min = $('fTo').min = META.sig_start;
    $('fTo').value = $('fFrom').max = $('fTo').max = META.last_date;

    chart = new KChart($('chart'), { onHover: i => legend(i) });
    bindUI();
    refresh(true);
  }

  const ymd = n => { const s = String(n); return `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}`; };

  // ---------- 篩選與排序
  function filterOK(s) {
    const from = $('fFrom').value || '0000', to = $('fTo').value || '9999';
    if (s.d < from || s.d > to) return false;
    const ty = $('fType').value;
    if (ty === 'first' && s.t !== 0) return false;
    if (ty === 'again' && s.t === 2) return false;
    if (ty === 'cont' && s.k < Math.max(2, +$('fN').value || 2)) return false;
    const mk = $('fMarket').value;
    if (mk !== '' && s.market !== +mk) return false;
    const q = $('fQ').value.trim();
    if (q && !s.code.startsWith(q) && !s.name.includes(q)) return false;
    return true;
  }

  function sorter() {
    const col = COLS.find(c => c.k === sortKey);
    const val = col.val || (s => s[sortKey]);
    const base = col.cmp || ((a, b) => {
      const x = val(a), y = val(b);
      return x < y ? -1 : x > y ? 1 : 0;
    });
    return (a, b) => {
      const x = col.cmp ? 0 : val(a), y = col.cmp ? 0 : val(b);
      if (!col.cmp && (x == null) !== (y == null)) return x == null ? 1 : -1;   // 空值永遠排最後
      const r = base(a, b) * sortDir;
      if (r) return r;
      return a.d < b.d ? 1 : a.d > b.d ? -1 : (a.code < b.code ? -1 : a.code > b.code ? 1 : 0);
    };
  }

  function refresh(keepSel) {
    const prev = view[sel];
    view = SIGS.filter(filterOK).sort(sorter());
    let i = keepSel && prev ? view.indexOf(prev) : -1;
    sel = i >= 0 ? i : 0;
    page = Math.floor(sel / PAGE);
    $('listCount').textContent = view.length.toLocaleString() + ' 筆';
    buildJump();
    renderStats();
    renderHead();
    renderBody();
    select(sel);
  }

  function typeLabel() {
    const ty = $('fType').value;
    return ty === 'first' ? '首次' : ty === 'again' ? '首次＋再次' : ty === 'all' ? '全部' : `連續 ${$('fN').value}+`;
  }

  function buildJump() {
    const cnt = new Map();
    for (const s of view) cnt.set(s.d, (cnt.get(s.d) || 0) + 1);
    const days = [...cnt.keys()].sort().reverse();
    const lab = typeLabel();
    $('fJump').innerHTML = '<option value="">選擇日期</option>' +
      days.map(d => `<option value="${d}">${d}（${lab} ${cnt.get(d)} 檔）</option>`).join('');
  }

  // ---------- 整體統計
  function renderStats() {
    const H = [5, 10, 20];
    const rows = H.map(h => {
      const xs = [], bs = [];
      for (const s of view) {
        const r = s['r' + h];
        if (r == null) continue;
        xs.push(r);
        if (s['b' + h] != null) bs.push(s['b' + h]);
      }
      xs.sort((a, b) => a - b);
      const n = xs.length;
      const mean = n ? xs.reduce((a, b) => a + b, 0) / n : null;
      const med = n ? (n % 2 ? xs[(n - 1) / 2] : (xs[n / 2 - 1] + xs[n / 2]) / 2) : null;
      const win = n ? xs.filter(x => x > 0).length / n : null;
      const bm = bs.length ? bs.reduce((a, b) => a + b, 0) / bs.length : null;
      return { n, mean, med, win, bm, ex: mean != null && bm != null ? mean - bm : null };
    });
    const stocks = new Set(view.map(s => s.code)).size;
    const cell = (f) => rows.map(f).join('');
    $('stats').innerHTML = `
      <div class="big">
        <div class="kv"><span>訊號數</span><b>${view.length.toLocaleString()}</b></div>
        <div class="kv"><span>個股數</span><b>${stocks.toLocaleString()}</b></div>
      </div>
      <div>
        <div class="title">整體統計 <span class="muted" style="font-weight:normal;font-size:12px">（依目前篩選條件）</span></div>
        <table>
          <tr><th></th><th>+5日</th><th>+10日</th><th>+20日</th></tr>
          <tr><td>樣本數</td>${cell(r => `<td>${r.n.toLocaleString()}</td>`)}</tr>
          <tr><td>平均報酬</td>${cell(r => `<td>${pctSpan(r.mean, 2)}</td>`)}</tr>
          <tr><td>中位數</td>${cell(r => `<td>${pctSpan(r.med, 2)}</td>`)}</tr>
          <tr><td>勝率（報酬 &gt; 0）</td>${cell(r => `<td>${r.win == null ? '—' : (r.win * 100).toFixed(1) + '%'}</td>`)}</tr>
          <tr><td>同期 0050 平均</td>${cell(r => `<td>${pctSpan(r.bm, 2)}</td>`)}</tr>
          <tr><td>超額（平均 − 0050）</td>${cell(r => `<td><b>${pctSpan(r.ex, 2)}</b></td>`)}</tr>
        </table>
      </div>
      <div class="note">樣本數只計入已走完該天數的訊號。超額為正，代表這些訊號平均跑贏同期大盤。</div>`;
  }

  // ---------- 表格
  function renderHead() {
    $('thead').innerHTML = COLS.map(c => {
      const on = c.k === sortKey;
      return `<th data-k="${c.k}" class="${c.l ? 'l' : ''} ${on ? 'sorted' : ''}">${c.t}${on ? (sortDir > 0 ? ' ▲' : ' ▼') : ''}</th>`;
    }).join('');
  }

  function renderBody() {
    const pages = Math.max(1, Math.ceil(view.length / PAGE));
    page = Math.min(Math.max(0, page), pages - 1);
    const rows = view.slice(page * PAGE, page * PAGE + PAGE);
    $('tbody').innerHTML = rows.length ? rows.map((s, j) => {
      const i = page * PAGE + j;
      return `<tr data-i="${i}" class="${i === sel ? 'sel' : ''}">` +
        COLS.map(c => `<td class="${c.l ? 'l' : ''} ${c.cls || ''}">${c.f(s)}</td>`).join('') + '</tr>';
    }).join('') : `<tr><td colspan="${COLS.length}" class="l muted" style="padding:20px">沒有符合條件的訊號</td></tr>`;
    // 換頁時表格高度不變（最後一頁筆數較少也一樣），畫面才不會跳
    const wrap = document.querySelector('.table-wrap');
    if (rows.length === PAGE) tableH = Math.max(tableH, wrap.offsetHeight);
    wrap.style.minHeight = pages > 1 && tableH ? tableH + 'px' : '';
    $('pgText').textContent = `第 ${page + 1} / ${pages} 頁`;
    $('pgPrev').disabled = page <= 0;
    $('pgNext').disabled = page >= pages - 1;
  }

  // ---------- 選取與 K 線圖
  function select(i) {
    if (!view.length) {
      $('chartCard').classList.add('hidden');
      return;
    }
    sel = Math.min(Math.max(0, i), view.length - 1);
    const p = Math.floor(sel / PAGE);
    if (p !== page) { page = p; renderBody(); }
    document.querySelectorAll('#tbody tr.sel').forEach(tr => tr.classList.remove('sel'));
    const tr = document.querySelector(`#tbody tr[data-i="${sel}"]`);
    if (tr) tr.classList.add('sel');   // 只標示，不捲動畫面
    $('navPos').textContent = `訊號清單 ${sel + 1} / ${view.length}`;
    $('prevBtn').disabled = sel <= 0;
    $('nextBtn').disabled = sel >= view.length - 1;
    showChart(view[sel]);
  }

  async function loadPx(code) {
    if (pxCache.has(code)) return pxCache.get(code);
    const js = await getJSON(`data/px/${code}.json`);
    const d0 = ymd(js.d0);
    let t = new Date(d0 + 'T00:00:00Z').getTime();
    const bars = js.dd.map((dd, i) => {
      t += dd * 86400000;
      return { t: new Date(t).toISOString().slice(0, 10), o: js.o[i], h: js.h[i], l: js.l[i], c: js.c[i], v: js.v[i] };
    });
    const c = bars.map(b => b.c), ma = {};
    for (const n of [5, 10, 20, 60, 120]) {
      const a = new Array(c.length).fill(null);
      let sum = 0;
      for (let i = 0; i < c.length; i++) {
        sum += c[i];
        if (i >= n) sum -= c[i - n];
        if (i >= n - 1) a[i] = sum / n;
      }
      ma[n] = a;
    }
    const px = { bars, ma, idx: new Map(bars.map((b, i) => [b.t, i])) };
    pxCache.set(code, px);
    if (pxCache.size > 40) pxCache.delete(pxCache.keys().next().value);
    return px;
  }

  async function showChart(s) {
    $('chartCard').classList.remove('hidden');
    curSig = s;
    $('cTitle').textContent = `${s.code} ${s.name}`;
    $('cTag').textContent = `${s.market ? '上櫃' : '上市'} ・ ${s.ind}`;
    $('cFacts').innerHTML =
      `<span>訊號日 <b>${s.d}</b></span><span>收盤 <b>${num(s.c)}</b></span><span>成交額 <b>${amtFmt(s.amt)}</b></span>` +
      `<span>${badge(s)}</span><span>+5日 ${pctSpan(s.r5)}</span><span>+10日 ${pctSpan(s.r10)}</span><span>+20日 ${pctSpan(s.r20)}</span>` +
      `<span>至今報酬 <b>${pctSpan(s.rn)}</b></span><span>至今最高 <b>${pctSpan(s.hi)}</b></span><span>至今最低 <b>${pctSpan(s.lo)}</b></span>`;
    let px;
    try { px = await loadPx(s.code); } catch (e) {
      $('cLegend').textContent = 'K 線資料載入失敗：' + e.message;
      return;
    }
    if (curSig !== s) return;   // 使用者已經換到別檔
    curPx = px;
    const si = px.idx.get(s.d);
    const other = SIGS.filter(x => x.code === s.code && x !== s && filterOK(x)).map(x => px.idx.get(x.d)).filter(i => i != null);
    chart.setData({
      bars: px.bars, ma: px.ma, maColors: MA_COLORS(), sigIdx: si, otherIdx: other,
      priceLine: si != null ? px.bars[si].c : null, from: Math.max(0, (si ?? px.bars.length - 1) - 60),
    });
    if (si != null && Math.abs(px.bars[si].c / s.c - 1) > 0.005) {
      $('cFacts').children[1].innerHTML = `收盤 <b>${num(s.c)}</b> <span class="muted">（還原 ${num(px.bars[si].c)}）</span>`;
    }
    legend(null);
  }

  function legend(i) {
    if (!curPx) return;
    const n = curPx.bars.length;
    if (i == null) i = n - 1;
    const b = curPx.bars[i], p = i > 0 ? curPx.bars[i - 1].c : null;
    const ch = p ? b.c / p - 1 : null;
    $('cLegend').innerHTML = `<b>${b.t}</b><span>開 ${num(b.o)}</span><span>高 ${num(b.h)}</span><span>低 ${num(b.l)}</span>` +
      `<span>收 <b class="${cls(ch)}">${num(b.c)}</b> ${pctSpan(ch, 2)}</span><span>量 ${b.v.toLocaleString()} 張</span>`;
    $('cMA').innerHTML = [5, 10, 20, 60, 120].map(k =>
      `<span style="color:var(--ma${k})">${k}MA ${num(curPx.ma[k][i])}</span>`).join('');
  }

  // ---------- 事件
  function bindUI() {
    ['fFrom', 'fTo', 'fMarket'].forEach(id => $(id).addEventListener('change', () => refresh(true)));
    $('fType').addEventListener('change', () => {
      $('fNWrap').classList.toggle('hidden', $('fType').value !== 'cont');
      refresh(true);
    });
    $('fN').addEventListener('input', () => refresh(true));
    let qt;
    $('fQ').addEventListener('input', () => { clearTimeout(qt); qt = setTimeout(() => refresh(false), 200); });
    $('fJump').addEventListener('change', () => {
      const d = $('fJump').value;
      if (!d) return;
      const i = view.findIndex(s => s.d === d);
      if (i >= 0) select(i);
    });
    $('thead').addEventListener('click', e => {
      const th = e.target.closest('th');
      if (!th) return;
      const k = th.dataset.k;
      if (k === sortKey) sortDir = -sortDir;
      else { sortKey = k; sortDir = ['d', 'r5', 'r10', 'r20', 'rn', 'hi', 'chg', 'amt', 'm5', 'm10', 'm20', 't'].includes(k) ? -1 : 1; }
      refresh(false);   // 換排序就回到第一筆
    });
    $('tbody').addEventListener('click', e => {
      const tr = e.target.closest('tr[data-i]');
      if (tr) select(+tr.dataset.i);
    });
    $('prevBtn').onclick = () => select(sel - 1);
    $('nextBtn').onclick = () => select(sel + 1);
    $('pgPrev').onclick = () => { page--; renderBody(); };
    $('pgNext').onclick = () => { page++; renderBody(); };
    document.addEventListener('keydown', e => {
      if (/^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName) && document.activeElement.type !== 'checkbox') return;
      if (e.key === 'ArrowLeft') { e.preventDefault(); select(sel - 1); }
      if (e.key === 'ArrowRight') { e.preventDefault(); select(sel + 1); }
    });
    $('theme').onclick = () => {
      const light = document.documentElement.dataset.theme !== 'light';
      if (light) document.documentElement.dataset.theme = 'light'; else delete document.documentElement.dataset.theme;
      try { localStorage.setItem('theme', light ? 'light' : 'dark'); } catch (e) {}
      $('themeLabel').textContent = light ? '日間' : '夜間';
      if (curSig) showChart(curSig);
    };
    if (document.documentElement.dataset.theme === 'light') $('themeLabel').textContent = '日間';
    $('checkBox').addEventListener('toggle', loadCheck, { once: true });
  }

  async function loadCheck() {
    let c;
    try { c = await getJSON('data/check.json'); } catch (e) { $('checkBody').textContent = '讀取失敗'; return; }
    const esc = s => String(s).replace(/[&<>]/g, m => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[m]));
    const big = [...c.big].sort((a, b) => a.date < b.date ? 1 : -1);
    let h = `<p>檢查時間 ${c.generated}。抓到新資料 ${c.fresh} / ${c.total} 檔`;
    h += c.reused.length ? `；沿用前一版資料 ${c.reused.length} 檔（${c.reused.slice(0, 30).join('、')}${c.reused.length > 30 ? '…' : ''}）` : '';
    h += c.nodata.length ? `；完全抓不到 ${c.nodata.length} 檔（${c.nodata.slice(0, 30).join('、')}${c.nodata.length > 30 ? '…' : ''}）` : '';
    h += '。</p>';
    h += `<p><b>單日漲跌超過 ±10.5%（台股漲跌幅上限 10%，可能是資料錯誤）：${big.length} 筆</b>` +
      `<br><span>和前一筆資料之間缺了交易日時，上限按天數放寬（缺 1 天約 ±21%）；已排除新上市前 5 天、停牌 3 天以上恢復交易後的前 5 天（這些日子沒有漲跌幅限制）。</span></p>`;
    if (big.length) {
      h += '<table><tr><th>日期</th><th>代號</th><th>公司</th><th>漲跌</th><th>官方比對</th></tr>' +
        big.slice(0, 100).map(x => `<tr><td>${x.date}</td><td>${x.code}</td><td>${esc(x.name)}</td><td class="${cls(x.ret)}">${pct(x.ret)}</td><td class="muted">${x.off === 'same' ? '官方資料也是這個價格' : x.off === 'none' ? '官方當天沒有這檔的成交' : '尚未和官方比對'}</td></tr>`).join('') +
        '</table>';
      if (big.length > 100) h += `<p>（只列最近 100 筆）</p>`;
    }
    if (c.repairs && c.repairs.length) {
      const days = c.repairs.filter(x => x.kind === '補缺日'), fix = c.repairs.filter(x => x.kind === '修正價格');
      h += `<p><b>用官方資料（證交所、櫃買中心每日收盤行情）修正：</b>補回 Yahoo 整個市場都缺的交易日 ${days.length} 天` +
        (days.length ? `（${days.map(x => `${x.date} ${x.count} 檔`).join('、')}）` : '') + `；修正價格 ${fix.length} 筆。</p>`;
      if (fix.length) h += '<table><tr><th>日期</th><th>代號</th><th>公司</th><th>Yahoo 收盤</th><th>官方收盤</th></tr>' +
        fix.slice(0, 100).map(x => `<tr><td>${x.date}</td><td>${x.code}</td><td>${esc(x.name)}</td><td>${x.yahoo}</td><td>${x.official}</td></tr>`).join('') + '</table>';
    }
    if (c.official && c.official.pending && c.official.pending.length)
      h += `<p>還有 ${c.official.pending.length} 個日子的官方資料這次沒抓完，下次更新會繼續。</p>`;
    if (c.bench_missing && c.bench_missing.length) h += `<p>0050（大盤對照）在 Yahoo 缺少的交易日：${c.bench_missing.join('、')}（這幾天開始或結束的訊號，同期 0050 欄位會空白）</p>`;
    h += `<p>最新交易日沒有成交資料（可能停牌）：${c.stale.length} 檔${c.stale.length ? '（' + c.stale.slice(0, 40).map(x => `${x.code} ${esc(x.name)}`).join('、') + '）' : ''}</p>`;
    h += `<p>期間內有缺少交易日的股票（多半是停牌）：${c.gaps_total} 檔${c.gaps.length ? '，缺最多的：' + c.gaps.slice(0, 15).map(x => `${x.code} ${esc(x.name)} ${x.days} 天`).join('、') : ''}</p>`;
    $('checkBody').innerHTML = h;
  }

  init();
})();
