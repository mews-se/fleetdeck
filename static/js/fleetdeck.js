/* Renders every page from its JSON, refreshes it on an interval and streams
   action output over SSE. One file, no framework. */
const FD = (() => {
  const REFRESH = { overview: 30, hosts: 30, guests: 30, containers: 60, network: 60, upstream: 120, actions: 30 };
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const dash = '<span class="dim">—</span>';
  const pill = (k, t) => `<span class="pill ${k}">${esc(t)}</span>`;
  const dot = (k) => `<span class="dot ${k} inline"></span>`;
  const bar = (p) => p == null ? dash : `<span class="bar ${p >= 95 ? 'crit' : p >= 85 ? 'warn' : ''}"><i style="width:${Math.min(100, p)}%"></i></span>${Math.round(p)} %`;
  const num = (v, d = 0) => v == null || Number.isNaN(Number(v)) ? '—' : Number(v).toFixed(d);
  const nowS = () => Date.now() / 1000;
  const span = (s) => {
    if (s == null) return '—';
    s = Math.max(0, s);
    if (s < 90) return `${Math.round(s)} s`;
    if (s < 5400) return `${Math.round(s / 60)} min`;
    if (s < 172800) return `${Math.round(s / 3600)} h`;
    return `${Math.round(s / 86400)} d`;
  };
  const age = (ts) => ts ? span(nowS() - ts) : '—';
  const when = (ts) => ts ? new Date(ts * 1000).toLocaleString(undefined, { weekday: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }) : '—';
  const dateOf = (iso) => iso ? new Date(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short' }) : '—';
  const gb = (b) => b == null ? '—' : `${(b / 1e9).toFixed(b >= 1e11 ? 0 : 1)} GB`;
  const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

  // Tables with inputs are only rebuilt when their data changed, so a typed
  // parameter survives the periodic refresh.
  const memo = {};
  const changed = (key, obj) => { const j = JSON.stringify(obj); if (memo[key] === j) return false; memo[key] = j; return true; };

  const stateKind = { running: 'good', up: 'good', stopped: 'off', template: 'off', exited: 'warn', paused: 'off', dead: 'crit', restarting: 'warn', created: 'off' };
  const statePill = (s) => pill(stateKind[s] || 'off', s || 'unknown');

  const charts = new Map();
  function chart(id, xs, series, opts = {}) {
    const el = $(id);
    if (!el || !window.uPlot) return;
    if (charts.has(id)) { charts.get(id).destroy(); charts.delete(id); }
    el.innerHTML = '';
    if (!xs || xs.length < 2) { el.innerHTML = '<div class="empty">no data yet</div>'; return; }
    const colors = [cssVar('--data'), cssVar('--data-2')];
    const height = el.clientHeight || 64;
    const u = new uPlot({
      width: el.clientWidth || 320,
      height,
      cursor: { show: false },
      legend: { show: false },
      padding: [4, 4, 4, 4],
      scales: { x: { time: true }, y: { range: (u, min, max) => [opts.min ?? Math.min(min, max), opts.max ?? max] } },
      axes: [{ show: false }, { show: false }],
      series: [{}, ...series.map((s, i) => ({ stroke: colors[i], width: 2, fill: i === 0 ? colors[i] + '22' : undefined, points: { show: false }, spanGaps: true }))],
    }, [xs, ...series], el);
    charts.set(id, u);
    if (!el.dataset.observed) {
      el.dataset.observed = '1';
      new ResizeObserver(() => { const c = charts.get(id); if (c) c.setSize({ width: el.clientWidth, height }); }).observe(el);
    }
  }

  /* ---------- action runs ---------- */
  let es = null;
  function termSpan(text) {
    const cls = text.startsWith('$ ') || text.startsWith('POST ') ? 'p' : text === 'exit 0' || text === 'task OK' ? 'ok' : /^(exit [1-9]|error:|killed|task .*(?:ERROR|FAIL)|\d{3}:)/.test(text) ? 'bad' : text.startsWith('waiting') || text.startsWith('task ') ? 'c' : '';
    const el = document.createElement('span');
    el.className = cls;
    el.textContent = text + '\n';
    return el;
  }
  // lines are appended in batches: a reflow per line freezes the tab on a
  // long backlog (a timer, not requestAnimationFrame, so hidden tabs keep up)
  const pending = new Map();
  function flushLines(term) {
    const lines = pending.get(term) || [];
    pending.delete(term);
    if (!lines.length) return;
    const frag = document.createDocumentFragment();
    lines.forEach((t) => frag.appendChild(termSpan(t)));
    term.appendChild(frag);
    term.scrollTop = term.scrollHeight;
  }
  function termLine(term, text) {
    if (!pending.has(term)) { pending.set(term, []); setTimeout(() => flushLines(term), 16); }
    pending.get(term).push(text);
  }
  function stream(runId, term, meta, label) {
    if (es) es.close();
    term.innerHTML = '';
    pending.delete(term);
    if (meta) meta.textContent = `${label} · run #${runId}`;
    es = new EventSource(`/api/actions/runs/${runId}/stream`);
    es.addEventListener('line', (e) => termLine(term, JSON.parse(e.data)));
    es.addEventListener('end', (e) => {
      es.close(); es = null;
      const exit = JSON.parse(e.data).exit;
      if (meta) meta.textContent = `${label} · run #${runId} · ${exit === 0 ? 'done' : 'exit ' + exit}`;
      refreshNav();
      if (view || page === 'actions' || page === 'guests' || page === 'containers') refresh();
    });
    es.onerror = () => { if (es) { es.close(); es = null; } };
  }
  async function refreshToken() {
    try {
      const r = await fetch('/api/token');
      if (r.ok) document.querySelector('meta[name=confirm-token]').content = (await r.json()).token;
    } catch (e) { /* the next page load issues a new one */ }
  }
  async function run(a, row, extra = {}) {
    const term = $('term'), meta = $('term-meta'), panel = $('term-panel');
    if (panel) panel.hidden = false;
    const sel = row && row.querySelector('select[data-target]');
    const target = extra.target || (sel ? sel.value : a.target);
    const params = { ...(extra.params || {}) };
    if (row) row.querySelectorAll('input[data-param]').forEach((i) => { if (!(i.dataset.param in params)) params[i.dataset.param] = i.value; });
    const what = Object.values(params).length ? ` (${Object.values(params).join(', ')})` : '';
    const summary = a.summary.replace(/\{([a-z_][a-z0-9_]*)\}/g, (m, k) => params[k] ?? m);
    let token = null;
    if (a.policy === 'confirm') {
      if (!confirm(`${a.title}${what} on ${target}?\n\n${summary}\n\nThis target is production. Run it?`)) return;
      token = document.querySelector('meta[name=confirm-token]').content;
    }
    let r;
    try {
      r = await fetch(`/api/actions/${a.id}/run`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ params, token, target }) });
    } catch (e) { term.innerHTML = ''; termLine(term, `error: ${e}`); return; }
    if (token) refreshToken();
    if (!r.ok) {
      let detail = r.statusText;
      try { detail = (await r.json()).detail || detail; } catch (e) { /* not JSON */ }
      term.innerHTML = ''; termLine(term, `error: ${r.status} ${detail}`);
      return;
    }
    const { run_id } = await r.json();
    stream(run_id, term, meta, `${a.title} · ${target}`);
  }
  const targetCell = (a) => a.targets && a.targets.length > 1
    ? `<select data-target>${a.targets.map((t) => `<option value="${esc(t.id)}">${esc(t.label)}</option>`).join('')}</select>`
    : esc(a.target_label);

  /* ---------- renderers ---------- */
  const R = {};

  R.overview = (d) => {
    $('stats').innerHTML = d.stats.map((s) => `<div class="stat ${s.kind || ''}"><span class="l">${esc(s.label)}</span><span class="v">${esc(s.value)}${s.of != null ? `<small>/${esc(s.of)}</small>` : ''}${s.unit ? ` <small>${esc(s.unit)}</small>` : ''}</span><span class="s">${esc(s.sub)}</span></div>`).join('');
    $('attn-meta').textContent = d.attention.length ? `${d.attention.length} item${d.attention.length === 1 ? '' : 's'}` : 'all clear';
    $('attn').innerHTML = d.attention.length ? d.attention.map((a) => `<li><span class="dot ${a.severity}"></span><div><div class="t">${esc(a.title)}</div><div class="d">${esc(a.detail || '')}${a.detail ? ' · ' : ''}since ${when(a.first_seen)}</div></div><span class="src">${esc(a.source)}</span></li>`).join('') : '<li class="empty">Nothing needs attention.</li>';
    const nas = d.nas;
    if (nas && nas.window) {
      $('nas-panel').hidden = false;
      const [sh, sm] = nas.window.start.split(':').map(Number), [eh, em] = nas.window.end.split(':').map(Number);
      const s = (sh * 60 + sm) / 1440 * 100, e = (eh * 60 + em) / 1440 * 100;
      const now = new Date(); const n = (now.getHours() * 60 + now.getMinutes()) / 1440 * 100;
      const spans = s <= e ? `<i style="left:${s}%;width:${e - s}%"></i>` : `<i style="left:0;width:${e}%"></i><i style="left:${s}%;width:${100 - s}%"></i>`;
      $('nas-track').innerHTML = `${spans}<b style="left:${n}%"></b>`;
      $('nas-meta').textContent = `${nas.ip || ''} · ${nas.window.start}–${nas.window.end}`;
      $('nas-kv').innerHTML = `<dt>Window</dt><dd>${nas.window.on ? `open, closes ${nas.window.end}` : `closed, opens ${nas.window.start}`}</dd><dt>Beszel</dt><dd>${esc(nas.status || 'no agent data')}</dd>`;
    }
    const w = d.wan;
    if (w) {
      $('wan-meta').textContent = `speedtest-tracker ${w.id} · 7 days`;
      chart('wan-chart', w.series[0], [w.series[1]], { min: 0 });
      const l = w.latest;
      $('wan-kv').innerHTML = l ? `<dt>Last test ${when(l.created_at)}</dt><dd>${l.status === 'completed' ? `${num(l.download)} ↓ · ${num(l.upload)} ↑ Mbit/s · ${num(l.ping, 1)} ms` : esc(l.status)}</dd><dt>Last 24 h</dt><dd>${w.day.count} tests · ${w.day.failed} failed</dd><dt>7 days</dt><dd>min ${num(w.week.min)} · max ${num(w.week.max)} Mbit/s</dd>` : '<dt>No results</dt><dd>—</dd>';
    } else {
      $('wan-meta').textContent = 'speedtest-tracker';
      $('wan-chart').innerHTML = '<div class="empty">not configured</div>';
      $('wan-kv').innerHTML = '';
    }
    $('moved').innerHTML = d.moved.length ? d.moved.map((m) => `<li><span class="dot ${m.kind === 'release' ? 'info' : 'off'}"></span><div><div class="t"><a href="${esc(m.url)}" target="_blank" rel="noopener">${esc(m.title)}</a></div><div class="d">${esc(m.detail)}</div></div><span class="src">${esc(m.kind)}</span></li>`).join('') : '<li class="empty">Nothing moved in the last 24 h.</li>';
    $('quick').innerHTML = d.quick.length ? d.quick.map((a) => `<button class="btn" data-quick="${esc(a.id)}">${esc(a.title)} <span class="pill ${a.policy}">${a.policy}</span></button>`).join('') : '<div class="empty">The catalog is empty.</div>';
    $('quick').querySelectorAll('[data-quick]').forEach((b) => { const a = d.quick.find((x) => x.id === b.dataset.quick); b.onclick = () => run({ ...a, summary: a.title }); });
    const src = Object.entries(d.sources).sort();
    const failing = src.filter(([, r]) => !r.ok && !r.skipped).length;
    $('sources-meta').textContent = `${src.length} polled · ${failing} failing`;
    $('sources-t').innerHTML = `<tr><th>Source</th><th>Last run</th><th class="num">Took</th><th>State</th></tr>` +
      src.map(([n, r]) => `<tr><td class="mono">${esc(n)}</td><td class="dim">${age(r.ts)} ago</td><td class="num">${r.duration_ms} ms</td><td>${r.skipped ? pill('off', 'skipped') : r.ok ? pill('good', 'ok') : pill('warn', 'error')}${!r.ok && r.error ? ` <span class="dim small">${esc(r.error.slice(0, 80))}</span>` : ''}</td></tr>`).join('') +
      Object.entries(d.unconfigured).map(([n, why]) => `<tr><td class="mono">${esc(n)}</td><td class="dim" colspan="2">${esc(why)}</td><td>${pill('off', 'not configured')}</td></tr>`).join('');
  };

  /* click a column header to sort; the choice is kept per browser */
  const SORT_KEY = 'fd.hosts.sort';
  let hostSort = null;
  try { hostSort = JSON.parse(localStorage.getItem(SORT_KEY)); } catch (e) { /* none */ }
  const statusRank = { down: 0, noagent: 1, up: 2, paused: 3, off: 4 };
  function sortHosts(rows) {
    if (!hostSort) return rows;
    const { key, dir } = hostSort;
    const ipnum = (ip) => (ip || '').split('.').length === 4 ? (ip.split('.').reduce((n, o) => n * 256 + Number(o), 0)) : null;
    const val = (h) => key === 'status' ? statusRank[h.status] : key === 'ipnum' ? ipnum(h.ip) : h[key];
    return [...rows].sort((a, b) => {
      const x = val(a), y = val(b);
      if (x == null && y == null) return 0;
      if (x == null) return 1;
      if (y == null) return -1;
      const r = typeof x === 'number' ? x - y : String(x).localeCompare(String(y));
      return dir === 'desc' ? -r : r;
    });
  }
  function th(label, key, cls = '') {
    const on = hostSort && hostSort.key === key;
    const arrow = on ? (hostSort.dir === 'desc' ? ' ▾' : ' ▴') : '';
    return `<th class="${cls}${key ? ' sortable' : ''}"${key ? ` data-sort="${key}"` : ''}>${label}${arrow}</th>`;
  }

  R.hosts = (d) => {
    const c = d.counts;
    $('hosts-meta').textContent = `${c.up} of ${c.total} up · ${c.off} off by rule · ${c.noagent} without agent`;
    const kind = { up: 'good', down: 'crit', off: 'off', noagent: 'warn', paused: 'off' };
    $('hosts-t').innerHTML = `<tr>${th('', 'status')}${th('Host', 'id')}${th('Address', 'ipnum')}${th('Site', 'site')}${th('Role', 'role')}${th('Agent', 'agent')}${th('CPU', 'cpu')}${th('Memory', 'mem')}${th('Disk', 'disk')}${th('Temp', 'temp', 'num')}${th('Up', 'uptime', 'num')}<th></th></tr>` +
      sortHosts(d.hosts).map((h) => `<tr>
        <td>${dot(kind[h.status])}</td><td><b>${h.rule ? `<a href="/hosts/${esc(h.id)}">${esc(h.id)}</a>` : esc(h.id)}</b></td><td class="mono">${esc(h.ip || '')}</td><td>${esc(h.site)}</td><td class="wrap">${esc(h.role)}</td>
        <td class="sub">${h.status === 'noagent' ? '<span class="dim">none</span>' : h.status === 'down' ? `<span class="pill crit">down ${age(h.down_since)}</span>` : h.status === 'off' && !h.agent ? '<span class="dim">off</span>' : esc(h.agent || '')}${h.kernel ? `<br><span class="small">${esc(h.kernel)}</span>` : ''}</td>
        <td>${h.status === 'up' ? bar(h.cpu) : dash}</td><td>${h.status === 'up' ? bar(h.mem) : dash}</td><td>${h.status === 'up' ? bar(h.disk) : dash}</td>
        <td class="num">${h.status === 'up' && h.temp ? `${num(h.temp)} °C` : '—'}</td><td class="num">${h.status === 'up' ? span(h.uptime) : '—'}</td>
        <td><span class="links">${h.beszel ? `<a href="${esc(h.beszel)}" target="_blank" rel="noopener">beszel</a>` : ''}${d.links.termix ? `<a href="${esc(d.links.termix)}" target="_blank" rel="noopener">termix</a>` : ''}</span></td></tr>`).join('');
    $('hosts-t').querySelectorAll('th[data-sort]').forEach((el) => {
      el.onclick = () => {
        const key = el.dataset.sort;
        const numeric = ['cpu', 'mem', 'disk', 'temp', 'uptime'].includes(key);
        // addresses read best ascending like the other text columns
        if (hostSort && hostSort.key === key) hostSort = hostSort.dir === 'asc' ? { key, dir: 'desc' } : null;
        else hostSort = { key, dir: numeric ? 'desc' : 'asc' };
        try { localStorage.setItem(SORT_KEY, JSON.stringify(hostSort)); } catch (e) { /* private mode */ }
        R.hosts(d);
      };
    });
  };

  R.guests = (d) => {
    $('pves').innerHTML = d.pves.map((p) => {
      const n = p.node;
      return `<div class="panel">
        <h3>${esc(p.host)} · ${esc(p.ip || '')} <span class="meta">${esc(p.version || 'no data')}${n.ts ? ` · ${age(n.ts)} ago` : ''} · ${p.pdm ? `<a href="${esc(p.pdm)}" target="_blank" rel="noopener">PDM</a> · ` : ''}<a href="${esc(p.url)}" target="_blank" rel="noopener">web UI</a></span></h3>
        ${n.ts ? `<dl class="kv" style="margin:0 0 10px"><dt>Node</dt><dd>cpu ${num(n.cpu)} % · mem ${num(n.mem_pct)} % · load ${(n.load || []).map((x) => num(x, 2)).join(' ') || '—'} · iowait ${num(n.iowait, 1)} % · root ${num(n.root_pct)} % · up ${span(n.uptime)}</dd></dl>` : ''}
        <div class="tw"><table><tr><th class="num">ID</th><th></th><th>Name</th><th>State</th><th>CPU</th><th>Memory</th><th class="num">Up</th><th>Note</th><th></th></tr>
        ${p.guests.length ? p.guests.map((g) => {
          const act = g.actions || {};
          const btn = g.status === 'running' && act.shutdown ? `<button class="btn sm" data-run="${esc(act.shutdown)}">Shut down</button>` : g.status === 'stopped' && act.start ? `<button class="btn sm primary" data-run="${esc(act.start)}">Start</button>` : g.status === 'template' ? '' : p.pdm ? `<a href="${esc(p.pdm)}" target="_blank" rel="noopener">PDM</a>` : '';
          return `<tr><td class="num">${g.vmid}</td><td class="dim">${g.type}</td><td><b>${esc(g.name || '')}</b></td><td>${statePill(g.status)}</td><td>${g.status === 'running' ? bar(g.cpu) : dash}</td><td>${g.status === 'running' ? bar(g.mem_pct) : dash}</td><td class="num">${g.status === 'running' ? span(g.uptime) : '—'}</td><td class="dim">${[g.free ? 'free' : '', g.host && g.host !== g.name ? g.host : '', (g.tags || '').replace(/;/g, ' ')].filter(Boolean).join(' · ')}</td><td class="r">${btn}</td></tr>`;
        }).join('') : `<tr><td colspan="9" class="empty">No data from this host yet.</td></tr>`}
        </table></div>
        ${p.storages.length ? `<div class="tw" style="margin-top:8px"><table><tr><th>Storage</th><th>Type</th><th>Used</th><th class="num">Size</th><th>Content</th></tr>${p.storages.map((s) => `<tr><td class="mono">${esc(s.name)}</td><td class="dim">${esc(s.plugin || '')}</td><td>${s.status === 'available' ? bar(s.pct) : pill('off', s.status || 'unknown')}</td><td class="num">${gb(s.maxdisk)}</td><td class="dim wrap">${esc(s.content || '')}</td></tr>`).join('')}</table></div>` : ''}
      </div>`;
    }).join('') || '<div class="panel empty">No PVE host configured.</div>';
    bindRuns(d.pves.flatMap((p) => p.guests.flatMap((g) => Object.entries(g.actions || {}).map(([op, id]) => ({ id, title: `${op === 'start' ? 'Start' : op === 'shutdown' ? 'Shut down' : op} ${g.type} ${g.vmid}`, target: p.host, policy: 'free', summary: `${op} ${g.vmid}` })))));
  };

  /* which container operations make sense in the row's current state */
  const opFits = (op, c) => ({ restart: c.state === 'running', stop: c.state === 'running', start: c.state !== 'running', update: !!c.update })[op] ?? true;
  const opButtons = (host, c) => (c.actions || []).filter((x) => opFits(x.op, c))
    .map((x) => `<button class="btn sm" data-run="${esc(x.id)}" data-target="${esc(host)}" data-container="${esc(c.name)}">${esc(x.op[0].toUpperCase() + x.op.slice(1))}${x.policy === 'confirm' ? '…' : ''}</button>`).join(' ');
  const containerTable = (host, rows) => `<div class="tw"><table><tr><th>Container</th><th>Image</th><th>State</th><th>Status</th><th>Update</th><th></th></tr>
    ${rows.map((c) => `<tr><td><b>${esc(c.name)}</b></td><td class="mono">${esc(c.image || '')}</td><td>${statePill(c.state)}</td><td class="dim">${esc(c.status || '')}</td><td>${c.update ? pill('info', c.update) : ''}</td><td class="r">${opButtons(host, c)}</td></tr>`).join('')}
  </table></div>`;
  const groupBody = (g) => g.off ? `<div class="panel off">Host is shut down${g.guest_status ? ` (${esc(g.guest_status)})` : ''}. Nothing to show until it is started.</div>`
    : g.env == null ? '<div class="panel off">Running, but not a Dockhand environment: its containers are not read yet.</div>'
    : g.containers.length ? `<div class="panel">${containerTable(g.host, g.containers)}</div>` : '<div class="panel off">No containers read yet from this environment.</div>';

  R.containers = (d) => {
    $('groups').innerHTML = d.groups.map((g) => {
      const sys = g.system;
      const how = g.env != null ? `Dockhand environment ${g.env}` : 'off by rule';
      const extra = sys ? ` · docker ${esc(sys.docker || '?')}${sys.containers ? ` · ${sys.containers.running}/${sys.containers.total} running` : ''}${sys.layers_size ? ` · images ${gb(sys.layers_size)}` : ''}${sys.vulns && sys.vulns.total ? ` · ${sys.vulns.total} vulnerabilities` : ''}` : '';
      return `<div class="hostgroup">
        <div class="hd"><b><a href="/hosts/${esc(g.host)}">${esc(g.host)}</a></b><span class="small">${esc(g.ip)}</span><span class="dim">${how}${extra}</span>
          <span class="links">${g.links.dockhand && g.env != null ? `<a href="${esc(g.links.dockhand)}" target="_blank" rel="noopener">Dockhand</a>` : ''}${g.off ? '<a href="/guests">guests</a>' : ''}</span></div>
        ${groupBody(g)}
      </div>`;
    }).join('') || '<div class="panel empty">No Docker host configured.</div>';
    bindRuns(d.actions || []);
  };

  R.network = (d) => {
    const k = d.kuma, c = k.counts;
    $('kuma-meta').textContent = k.ts ? `${k.version ? 'v' + k.version + ' · ' : ''}${c.up}/${c.total} up · ${c.down} down · ${c.maintenance} maintenance · ${age(k.ts)} ago` : 'not read yet';
    const kk = { 0: 'crit', 1: 'good', 2: 'warn', 3: 'off' };
    $('kuma-t').innerHTML = `<tr><th></th><th>Monitor</th><th>Type</th><th>Target</th><th class="num">RTT</th></tr>` +
      (k.monitors.length ? k.monitors.map((m) => `<tr><td>${dot(kk[m.status] || 'off')}</td><td><b>${esc(m.name)}</b></td><td class="dim">${esc(m.type || '')}</td><td class="mono">${esc(m.url || [m.hostname, m.port].filter(Boolean).join(':'))}</td><td class="num">${m.status === 1 && m.rtt != null ? `${num(m.rtt)} ms` : esc(m.state || '')}</td></tr>`).join('') : '<tr><td colspan="5" class="empty">No monitors read yet.</td></tr>') +
      (k.url ? `<tr><td colspan="5" class="r"><a href="${esc(k.url)}" target="_blank" rel="noopener">open Uptime Kuma</a></td></tr>` : '');
    const a = d.adguard, s = a.stats;
    $('ag-meta').textContent = a.ts ? `${a.status.version || ''} · ${age(a.ts)} ago` : 'not read yet';
    if (s.hourly_queries) {
      const n = s.hourly_queries.length, t0 = Math.floor(nowS() / 3600) * 3600 - (n - 1) * 3600;
      chart('ag-chart', s.hourly_queries.map((_, i) => t0 + i * 3600), [s.hourly_queries, s.hourly_blocked || []], { min: 0 });
    } else $('ag-chart').innerHTML = '<div class="empty">no data yet</div>';
    $('ag-kv').innerHTML = a.ts ? `<dt>Queries 24 h</dt><dd>${num(s.queries)}</dd><dt>Blocked</dt><dd>${num(a.blocked_pct, 1)} %</dd><dt>Upstream avg</dt><dd>${num(s.avg_ms)} ms</dd><dt>Top blocked</dt><dd>${(s.top_blocked || []).slice(0, 3).map((x) => esc(x.name)).join(' · ') || '—'}</dd><dt>Top clients</dt><dd>${(s.top_clients || []).slice(0, 3).map((x) => `${esc(x.name)} (${x.count})`).join(' · ') || '—'}</dd>${a.url ? `<dt></dt><dd><a href="${esc(a.url)}" target="_blank" rel="noopener">open AdGuard Home</a></dd>` : ''}` : '';
    $('st').innerHTML = d.speedtests.map((st) => `<div class="panel" style="margin-top:16px">
      <h3>WAN · ${esc(st.site)} <span class="meta">speedtest-tracker ${esc(st.id)} · 7 days${st.latest ? ` · last ${when(st.latest.created_at)}` : ''}</span></h3>
      <div class="chart tall" id="st-${esc(st.id)}"></div>
      <div class="legend"><span><i class="data"></i>download Mbit/s</span><span><i class="data-2"></i>upload</span><span>${st.week.count} tests · ${st.week.failed} failed · min ${num(st.week.min)} · max ${num(st.week.max)}</span>${st.url ? `<a href="${esc(st.url)}" target="_blank" rel="noopener">open</a>` : ''}</div>
    </div>`).join('');
    d.speedtests.forEach((st) => chart(`st-${st.id}`, st.series[0], [st.series[1], st.series[2]], { min: 0 }));
  };

  R.upstream = (d) => {
    const c = d.counts;
    $('up-meta').textContent = `${c.open} open · ${d.threads.length} watched`;
    const sk = { open: 'good', merged: 'info', closed: 'off' };
    $('up-t').innerHTML = `<tr><th></th><th>Repo</th><th>Ref</th><th>Title</th><th>Updated</th><th class="num">Comments</th><th>State</th></tr>` +
      (d.threads.length ? d.threads.map((t) => `<tr><td>${dot(t.moved ? 'info' : sk[t.state] || 'off')}</td><td class="sub">${esc(t.repo)}</td><td class="mono"><a href="${esc(t.url)}" target="_blank" rel="noopener">#${t.number}</a></td><td class="wrap">${esc(t.title)}</td><td class="dim">${dateOf(t.updated_at)}${t.moved ? ' ' + pill('info', 'moved') : ''}</td><td class="num">${t.comments}</td><td>${pill(sk[t.state] || 'off', t.state)}</td></tr>`).join('') : '<tr><td colspan="7" class="empty">No threads read yet.</td></tr>');
    const rk = { current: 'good', update: 'info', unknown: 'off' };
    $('rel-t').innerHTML = `<tr><th></th><th>Repo</th><th>Running</th><th>Latest</th><th>Source</th></tr>` +
      (d.releases.length ? d.releases.map((r) => `<tr><td>${dot(rk[r.state])}</td><td class="sub">${esc(r.repo)}</td><td class="mono">${esc(r.running_version || '?')}</td><td class="mono"><a href="${esc(r.url || '#')}" target="_blank" rel="noopener">${esc(r.latest_tag || '?')}</a>${r.state === 'update' ? ' ' + pill('info', 'update') : ''}</td><td class="dim">${esc(r.running_source || '')}${r.published_at ? ` · ${dateOf(r.published_at)}` : ''}</td></tr>`).join('') : '<tr><td colspan="5" class="empty">No releases read yet.</td></tr>');
  };

  const actionRow = (a) => `<tr data-action="${esc(a.id)}"><td><b>${esc(a.title)}</b><br><span class="small">${esc(a.summary)}</span>${a.params.map((p) => ` <label class="small">${esc(p.name)} <input data-param="${esc(p.name)}" value="${esc(p.default)}" pattern="${esc(p.pattern)}"></label>`).join('')}${a.note ? `<br><span class="dim">${esc(a.note)}</span>` : ''}</td><td class="sub">${targetCell(a)}</td><td>${pill(a.policy, a.policy)}</td><td class="num dim">${a.last ? `<a href="/actions/runs/${a.last.id}" title="exit ${a.last.exit}">${age(a.last.ts)}</a>${a.last.exit === 0 ? '' : a.last.exit == null ? ' ⋯' : ' ✗'}` : '—'}</td><td class="r"><button class="btn sm ${a.policy === 'free' ? 'primary' : ''}" data-run="${esc(a.id)}">${a.policy === 'confirm' ? 'Run…' : 'Run'}</button></td></tr>`;
  const actionTable = (actions, empty) => `<tr><th>Action</th><th>Target</th><th>Policy</th><th class="num">Last</th><th></th></tr>` +
    (actions.length ? actions.map(actionRow).join('') : `<tr><td colspan="5" class="empty">${empty}</td></tr>`);
  const runsTable = (runs) => `<tr><th class="num">#</th><th>Action</th><th>Target</th><th>Started</th><th class="num">Took</th><th>Exit</th></tr>` +
    (runs.length ? runs.map((r) => `<tr><td class="num"><a href="/actions/runs/${r.id}">${r.id}</a></td><td>${esc(r.action_id)}</td><td class="sub">${esc(r.target)}</td><td class="dim">${when(r.started_ts)}</td><td class="num">${r.finished_ts ? span(r.finished_ts - r.started_ts) : '⋯'}</td><td>${r.exit_code == null ? pill('info', 'running') : r.exit_code === 0 ? pill('good', '0') : pill('crit', String(r.exit_code))}</td></tr>`).join('') : '<tr><td colspan="6" class="empty">Nothing has run yet.</td></tr>');

  R.actions = (d) => {
    if (changed('catalog', d.actions)) {
      $('act-t').innerHTML = actionTable(d.actions, 'The catalog is empty. Add entries to config/catalog.yml.');
      bindRuns(d.actions);
    }
    $('runs-meta').textContent = d.running.length ? `${d.running.length} running` : '';
    $('runs-t').innerHTML = runsTable(d.runs);
    $('term-links').innerHTML = d.termix ? d.hosts.map((h) => `<a class="btn sm" href="${esc(d.termix)}" target="_blank" rel="noopener">${esc(h.id)}</a>`).join('') : '<span class="dim">no termix link in the config</span>';
  };

  R.host = (d) => {
    const h = d.host, s = d.system, kind = { up: 'good', down: 'crit', off: 'off', noagent: 'warn', paused: 'off' };
    const ruleKind = { test: 'good', prod: 'warn', readonly: 'off' };
    $('host-hd').innerHTML = `${dot(kind[h.status])}<b>${esc(h.id)}</b><span class="small">${esc(h.ip || '')}</span><span class="dim">${esc(h.site)}${h.role ? ` · ${esc(h.role)}` : ''} · ${pill(ruleKind[h.rule], h.rule)}${h.ssh ? ` · ${esc(h.ssh)}` : ''}</span><span class="links">${Object.entries(d.links).map(([k, v]) => `<a href="${esc(v)}" target="_blank" rel="noopener">${esc(k)}</a>`).join('')}</span>`;
    const status = h.status === 'up' ? 'agent connected' : h.status === 'down' ? `down ${age(s && s.down_since)}` : h.status === 'off' ? 'off by rule' : h.status === 'noagent' ? 'no agent' : h.status;
    $('sys-meta').textContent = s ? `Beszel ${esc(h.beszel)} · ${age(s.snap_ts)} ago` : 'no agent';
    let kv = `<dt>Status</dt><dd>${esc(status)}</dd>`;
    if (s) kv += `<dt>Hostname</dt><dd>${esc(s.hostname || '—')}</dd><dt>OS</dt><dd>${esc(s.os || '—')}</dd><dt>Kernel</dt><dd>${esc(s.kernel || '—')}</dd><dt>CPU</dt><dd class="wrap">${esc(s.model || '—')}${s.cores ? ` · ${s.cores} cores` : ''}${s.threads && s.threads !== s.cores ? ` / ${s.threads} threads` : ''}</dd><dt>Load</dt><dd>${(s.load || []).map((x) => num(x, 2)).join(' ') || '—'}</dd><dt>CPU use</dt><dd>${h.status === 'up' ? bar(s.cpu) : '—'}</dd><dt>Memory</dt><dd>${h.status === 'up' ? bar(s.mem) : '—'}</dd><dt>Disk</dt><dd>${h.status === 'up' ? bar(s.disk) : '—'}${Object.entries(s.extra_fs || {}).map(([m, p]) => `<br>${esc(m)} ${bar(p)}`).join('')}</dd><dt>Temp</dt><dd>${h.status === 'up' && s.temp ? `${num(s.temp)} °C` : '—'}</dd><dt>Uptime</dt><dd>${h.status === 'up' ? span(s.uptime) : '—'}</dd><dt>Agent</dt><dd>${esc(s.agent || '—')}</dd>`;
    else kv += `<dt>Beszel</dt><dd>${h.beszel ? `no data for ${esc(h.beszel)} yet` : 'no agent configured'}</dd>`;
    const p = d.pve_node;
    if (p) kv += `<dt>PVE</dt><dd>${esc(p.version || '?')} · ${p.running ?? '—'}/${p.guests ?? '—'} guests running · root ${num(p.root_pct)} % · <a href="${esc(p.url)}" target="_blank" rel="noopener">web UI</a> · <a href="/guests">guests</a></dd>`;
    $('sys-kv').innerHTML = kv;
    chart('ch-cpu', d.series.cpu[0], [d.series.cpu[1]], { min: 0, max: 100 });
    chart('ch-mem', d.series.mem[0], [d.series.mem[1]], { min: 0, max: 100 });
    chart('ch-temp', d.series.temp[0], [d.series.temp[1]], { min: 0 });
    const kk = { 0: 'crit', 1: 'good', 2: 'warn', 3: 'off' };
    $('mon-meta').textContent = d.monitors.length ? `Uptime Kuma · ${d.monitors.filter((m) => m.status === 1).length}/${d.monitors.length} up` : 'Uptime Kuma';
    $('mon-t').innerHTML = d.monitors.length ? `<tr><th></th><th>Monitor</th><th>Target</th><th class="num">RTT</th></tr>` + d.monitors.map((m) => `<tr><td>${dot(kk[m.status] || 'off')}</td><td><b>${esc(m.name)}</b><br><span class="small">${esc(m.type || '')}</span></td><td class="mono">${esc(m.url || [m.hostname, m.port].filter(Boolean).join(':'))}</td><td class="num">${m.status === 1 && m.rtt != null ? `${num(m.rtt)} ms` : esc(m.state || '')}</td></tr>`).join('') : '<tr><td class="empty">No monitor points at this host.</td></tr>';
    $('dns-kv').innerHTML = `<dt>DNS today</dt><dd>${d.dns.queries != null ? `${num(d.dns.queries)} queries via AdGuard` : 'not among AdGuard’s top clients'}</dd>`;
    const g = d.guest;
    $('guest-panel').hidden = !g;
    let extra = [];
    if (g) {
      const act = g.actions || {};
      const btn = g.status === 'running' && act.shutdown ? `<button class="btn sm" data-run="${esc(act.shutdown)}">Shut down</button>` : g.status === 'stopped' && act.start ? `<button class="btn sm primary" data-run="${esc(act.start)}">Start</button>` : '';
      $('guest-meta').innerHTML = `${esc(g.type)} ${g.vmid} on <a href="/hosts/${esc(g.node)}">${esc(g.node)}</a>${g.links.pdm ? ` · <a href="${esc(g.links.pdm)}" target="_blank" rel="noopener">PDM</a>` : ''} · <a href="${esc(g.links.pve)}" target="_blank" rel="noopener">web UI</a>`;
      const c = g.config;
      $('guest-kv').innerHTML = `<dt>State</dt><dd>${statePill(g.status)} ${btn}</dd><dt>CPU</dt><dd>${g.status === 'running' ? bar(g.cpu) : '—'}</dd><dt>Memory</dt><dd>${g.status === 'running' ? bar(g.mem_pct) : '—'}${g.maxmem ? ` of ${gb(g.maxmem)}` : ''}</dd><dt>Uptime</dt><dd>${g.status === 'running' ? span(g.uptime) : '—'}</dd><dt>Note</dt><dd>${esc([g.free ? 'free guest' : '', (g.tags || '').replace(/;/g, ' ')].filter(Boolean).join(' · ') || '—')}</dd>` +
        (c ? `<dt>Config</dt><dd>${c.cores ?? '?'} cores${c.sockets > 1 ? ` × ${c.sockets}` : ''} · ${c.memory ?? '?'} MiB${c.swap ? ` + ${c.swap} swap` : ''}${c.ostype ? ` · ${esc(c.ostype)}` : ''}${c.unprivileged ? ' · unprivileged' : ''}</dd><dt>Boot</dt><dd>${c.onboot ? 'on boot' : 'manual'}${c.startup ? ` · ${esc(c.startup)}` : ''}${c.agent ? ' · guest agent' : ''}</dd>` : '<dt>Config</dt><dd>not read yet</dd>');
      $('guest-t').innerHTML = c ? `<tr><th>NIC</th><th>MAC</th><th>Bridge</th><th>IP</th></tr>${(c.nics || []).map((n) => `<tr><td class="mono">${esc(n.name)}${n.model ? ` <span class="dim">${esc(n.model)}</span>` : ''}</td><td class="mono">${esc(n.mac || '—')}</td><td class="mono">${esc(n.bridge || '—')}${n.vlan ? ` tag ${n.vlan}` : ''}</td><td class="mono">${esc(n.ip || '—')}</td></tr>`).join('')}<tr><th>Disk</th><th>Volume</th><th>Storage</th><th class="num">Size</th></tr>${(c.disks || []).map((x) => `<tr><td class="mono">${esc(x.name)}</td><td class="mono">${esc(x.volume || '')}</td><td class="mono">${esc(x.storage || '—')}</td><td class="num">${esc(x.size || '—')}</td></tr>`).join('')}` : '';
      extra = Object.entries(act).map(([op, id]) => ({ id, title: `${op === 'start' ? 'Start' : op === 'shutdown' ? 'Shut down' : op} ${g.type} ${g.vmid}`, target: g.node, policy: 'free', summary: `${op} ${g.vmid}`, params: [] }));
    }
    const cg = d.containers;
    $('cont-panel').hidden = !cg;
    if (cg) {
      $('cont-meta').innerHTML = cg.env != null ? `Dockhand environment ${cg.env}${cg.system && cg.system.docker ? ` · docker ${esc(cg.system.docker)}` : ''}${cg.links.dockhand ? ` · <a href="${esc(cg.links.dockhand)}" target="_blank" rel="noopener">Dockhand</a>` : ''}` : 'off by rule';
      $('cont-body').innerHTML = groupBody(cg);
    }
    if (changed('host-catalog', d.actions)) $('act-t').innerHTML = actionTable(d.actions, 'No catalog entry targets this host.');
    $('runs-meta').textContent = d.running.length ? `${d.running.length} running` : '';
    $('runs-t').innerHTML = runsTable(d.runs);
    bindRuns(d.actions.concat(extra));
  };

  R.run = (d) => {
    const r = d.run;
    $('run-title').textContent = `${d.title} on ${r.target}`;
    $('run-kv').innerHTML = `<dt>Command</dt><dd>${esc(r.summary)}</dd><dt>Started</dt><dd>${new Date(r.started_ts * 1000).toLocaleString(undefined, { hourCycle: 'h23' })}${r.requested_by ? ` by ${esc(r.requested_by)}` : ''}${r.confirmed ? ' · confirmed' : ''}</dd><dt>Result</dt><dd>${r.finished_ts ? `exit ${r.exit_code} after ${span(r.finished_ts - r.started_ts)}` : 'running'}</dd>`;
    const term = $('term');
    if (d.live) stream(r.id, term, $('term-meta'), d.title);
    else { term.innerHTML = ''; d.lines.forEach((l) => termLine(term, l)); $('term-meta').textContent = r.finished_ts ? 'finished' : ''; }
  };

  function bindRuns(actions) {
    document.querySelectorAll('[data-run]').forEach((b) => {
      const a = actions.find((x) => x.id === b.dataset.run);
      if (a) b.onclick = () => run(a, b.closest('tr'), { target: b.dataset.target, params: b.dataset.container ? { container: b.dataset.container } : undefined });
    });
  }

  /* ---------- page plumbing ---------- */
  let page = null, data = null, render = null, view = null;
  async function refresh() {
    try {
      const r = await fetch(view || `/api/view/${page}`);
      if (!r.ok) return;
      data = await r.json();
      R[render](data);
    } catch (e) { /* offline; the next tick tries again */ }
  }
  async function refreshNav() {
    try {
      const r = await fetch('/api/nav');
      if (!r.ok) return;
      const { nav, chips } = await r.json();
      Object.entries(nav).forEach(([id, v]) => { const el = document.querySelector(`[data-nav="${id}"] .n`); if (el) { el.textContent = v.n; el.classList.toggle('warn', !!v.warn); } });
      $('chips').innerHTML = Object.entries(chips).map(([n, c]) => `<span class="chip" data-chip="${n}"><span class="dot ${c.kind}"></span>${esc(c.text)}</span>`).join('');
    } catch (e) { /* same */ }
  }
  function clock() { const el = $('clock'); if (el) el.textContent = new Date().toLocaleString(undefined, { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }); }
  function events() {
    const src = new EventSource('/api/events');
    src.addEventListener('attention', () => { refreshNav(); if (page === 'overview') refresh(); });
    src.addEventListener('source', () => refreshNav());
    src.addEventListener('run', () => { if (page === 'actions') refresh(); });
    src.onerror = () => { src.close(); setTimeout(events, 15000); };
  }
  /* name = the section for nav and refresh rate; renderer and view override
     the renderer and the JSON endpoint for pages that are not a section */
  function init(name, renderer, viewUrl) {
    page = name;
    render = renderer || name;
    view = viewUrl || null;
    data = JSON.parse($('data').textContent);
    R[render](data);
    clock();
    setInterval(clock, 30000);
    if (view || !renderer) {
      setInterval(refresh, (REFRESH[name] || 60) * 1000);
      setInterval(refreshNav, 60000);
    }
    events();
  }
  return { init, run, refresh };
})();
