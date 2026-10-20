// Reports page: sortable tables, deal sparklines, price-history charts and the flips price index.
// Charts are plain SVG built from JSON in data attributes; prices are copper. No dependencies.
(function () {
  const NS = "http://www.w3.org/2000/svg";
  const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const esc = (t) => String(t).replace(/[&<>"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]));
  const money = (c) => {
    if (c == null) return "–";
    const a = Math.abs(c), sign = c < 0 ? "−" : "";
    const g = Math.floor(a / 10000), s = Math.floor(a / 100) % 100, cu = a % 100;
    if (g >= 100) return `${sign}${g.toLocaleString()}g`;
    if (g) return `${sign}${g}g ${String(s).padStart(2, "0")}s`;
    if (s) return `${sign}${s}s ${String(cu).padStart(2, "0")}c`;
    return `${sign}${cu}c`;
  };
  const when = (t) => new Date(t * 1000).toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric",
                                                                      hour: "2-digit", minute: "2-digit" });
  const el = (name, attrs, parent) => {
    const n = document.createElementNS(NS, name);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(n);
    return n;
  };

  // ---- one shared tooltip
  const tip = document.createElement("div");
  tip.className = "tip";
  tip.hidden = true;
  document.body.appendChild(tip);
  function showTip(html, x, y) {
    tip.innerHTML = html;
    tip.hidden = false;
    const r = tip.getBoundingClientRect();
    let left = x + 14, top = y + 14;
    if (left + r.width > innerWidth - 8) left = x - r.width - 14;
    if (top + r.height > innerHeight - 8) top = y - r.height - 14;
    tip.style.left = `${Math.max(8, left)}px`;
    tip.style.top = `${Math.max(8, top)}px`;
  }
  const hideTip = () => { tip.hidden = true; };

  // ---- sortable tables: th[data-sort=num|text], cells carry data-v
  document.querySelectorAll("table.sortable").forEach((table) => {
    const ths = [...table.tHead.rows[0].cells];
    ths.forEach((th, col) => {
      if (!th.dataset.sort) return;
      th.tabIndex = 0;
      const sort = () => {
        const cur = th.getAttribute("aria-sort");  // numbers start high-to-low, text A-Z
        const desc = cur ? cur === "ascending" : th.dataset.sort === "num";
        ths.forEach((h) => h.removeAttribute("aria-sort"));
        th.setAttribute("aria-sort", desc ? "descending" : "ascending");
        const body = table.tBodies[0];
        const key = (tr) => {
          const v = tr.cells[col].dataset.v;
          return th.dataset.sort === "num" ? (v === "" || v == null ? null : Number(v)) : (v || "").toLowerCase();
        };
        const rows = [...body.rows].map((tr) => [key(tr), tr]);
        rows.sort(([a], [b]) => {
          if (a === b) return 0;
          if (a === null) return 1;   // blanks last either way
          if (b === null) return -1;
          return (a < b ? -1 : 1) * (desc ? -1 : 1);
        });
        rows.forEach(([, tr]) => body.appendChild(tr));
      };
      th.addEventListener("click", sort);
      th.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); sort(); } });
    });
  });

  // ---- local times
  document.querySelectorAll("[data-ts]").forEach((td) => { td.textContent = when(Number(td.dataset.ts)); });

  // Step-line path through [[x, y], ...] (each value holds until the next point), ending at xEnd.
  function stepPath(pts, xEnd) {
    let d = "";
    pts.forEach(([x, y], i) => {
      d += i ? `H${x.toFixed(1)}V${y.toFixed(1)}` : `M${x.toFixed(1)},${y.toFixed(1)}`;
    });
    return pts.length ? d + `H${xEnd.toFixed(1)}` : "";
  }

  // ---- deal sparklines: lowest listing over the window, dashed market value
  document.querySelectorAll(".sparkline").forEach((box) => {
    const pts = JSON.parse(box.dataset.points || "[]");
    const ref = Number(box.dataset.ref), now = Number(box.dataset.now);
    const W = 120, H = 30, pad = 3;
    const tNow = Date.now() / 1000;
    if (!pts.length) pts.push([tNow - 3600, now]);
    const t0 = pts[0][0], t1 = Math.max(tNow, pts[pts.length - 1][0] + 1);
    const vals = pts.map((p) => p[1]).concat([ref]);
    const lo = Math.min(...vals), hi = Math.max(...vals);
    const x = (t) => ((t - t0) / (t1 - t0)) * W;
    const y = (v) => (hi === lo ? H / 2 : pad + (1 - (v - lo) / (hi - lo)) * (H - 2 * pad));
    const svg = el("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img",
                            "aria-label": `Lowest listing ${money(lo)} to ${money(hi)}; market ${money(ref)}` }, box);
    el("line", { x1: 0, x2: W, y1: y(ref), y2: y(ref), stroke: "var(--muted)", "stroke-dasharray": "2 3", "stroke-width": 1 }, svg);
    const xy = pts.map(([t, v]) => [x(t), y(v)]);
    el("path", { d: stepPath(xy, W), fill: "none", stroke: "var(--s1)", "stroke-width": 1.75, "stroke-linejoin": "round" }, svg);
    el("circle", { cx: W, cy: xy[xy.length - 1][1], r: 2.5, fill: "var(--s1)" }, svg);
    const dot = el("circle", { r: 3, fill: "var(--card)", stroke: "var(--s1)", "stroke-width": 1.5, visibility: "hidden" }, svg);
    svg.addEventListener("mousemove", (e) => {
      const r = svg.getBoundingClientRect();
      const t = t0 + ((e.clientX - r.left) / r.width) * (t1 - t0);
      let i = 0;
      while (i + 1 < pts.length && pts[i + 1][0] <= t) i++;
      dot.setAttribute("cx", Math.max(0, Math.min(W, e.clientX - r.left)));
      dot.setAttribute("cy", xy[i][1]);
      dot.setAttribute("visibility", "visible");
      showTip(`<div class="t">${esc(when(t))}</div>lowest <b>${money(pts[i][1])}</b><br><span class="muted">market ${money(ref)}</span>`,
              e.clientX, e.clientY);
    });
    svg.addEventListener("mouseleave", () => { dot.setAttribute("visibility", "hidden"); hideTip(); });
  });

  // ---- price history: min / p10 / median step lines, units listed below (its own scale), crosshair
  const SERIES = [["min", "Lowest", "var(--s1)"], ["p10", "P10", "var(--s2)"], ["median", "Median", "var(--s3)"]];
  function niceTicks(lo, hi, n) {
    const span = hi - lo || Math.abs(hi) || 1;
    const step0 = span / n, mag = 10 ** Math.floor(Math.log10(step0));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= step0);
    const out = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(v);
    return out;
  }
  document.querySelectorAll(".pchart").forEach((box) => {
    const pts = JSON.parse(box.dataset.points);
    if (!pts.length) return;
    const legend = document.createElement("div");
    legend.className = "legend-row";
    legend.innerHTML = SERIES.map(([, label, c]) => `<span><span class="sw" style="background:${c}"></span>${label}</span>`).join("") +
      `<span><span class="sw" style="background:var(--bar);height:8px"></span>Units listed</span>`;
    box.before(legend);
    const W = Math.max(320, box.clientWidth), H = 260, top = 8, priceH = 170, gap = 18, qtyH = 50, left = 64, right = 12;
    const tNow = Date.now() / 1000;
    const t0 = pts[0].t, t1 = Math.max(tNow, pts[pts.length - 1].t + 1);
    const vals = pts.flatMap((p) => SERIES.map(([k]) => p[k]).filter((v) => v != null));
    let lo = Math.min(...vals), hi = Math.max(...vals);
    const padv = (hi - lo) * 0.08 || hi * 0.05 || 1;
    lo = Math.max(0, lo - padv); hi += padv;
    const qmax = Math.max(1, ...pts.map((p) => p.listed));
    const x = (t) => left + ((t - t0) / (t1 - t0)) * (W - left - right);
    const y = (v) => top + (1 - (v - lo) / (hi - lo)) * priceH;
    const qy0 = top + priceH + gap + qtyH, qy = (q) => qy0 - (q / qmax) * qtyH;
    const svg = el("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Price history chart" }, box);
    const axis = el("g", { class: "axis" }, svg);
    for (const v of niceTicks(lo, hi, 4)) {
      el("line", { x1: left, x2: W - right, y1: y(v), y2: y(v) }, axis);
      el("text", { x: left - 8, y: y(v) + 4, "text-anchor": "end" }, axis).textContent = money(Math.round(v));
    }
    el("line", { x1: left, x2: W - right, y1: qy0, y2: qy0 }, axis);
    el("text", { x: left - 8, y: qy(qmax) + 8, "text-anchor": "end" }, axis).textContent = qmax.toLocaleString();
    // time ticks: midnights (or every 6h for short windows)
    const spanH = (t1 - t0) / 3600, stepH = spanH <= 36 ? 6 : spanH <= 96 ? 24 : spanH <= 240 ? 48 : 96;
    const start = new Date(t0 * 1000); start.setMinutes(0, 0, 0);
    while (start.getHours() % Math.min(stepH, 24) !== 0 || (stepH >= 24 && start.getHours() !== 0)) start.setHours(start.getHours() + 1);
    for (let d = new Date(start); d / 1000 <= t1; d.setHours(d.getHours() + stepH)) {
      const tx = x(d / 1000);
      el("line", { x1: tx, x2: tx, y1: top, y2: qy0, "stroke-dasharray": "2 4" }, axis);
      el("text", { x: tx, y: H - 2, "text-anchor": "middle" }, axis).textContent = stepH >= 24
        ? d.toLocaleDateString(undefined, { weekday: "short", day: "numeric" })
        : d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
    }
    // listed units: step area
    const qpts = pts.map((p) => [x(p.t), qy(p.listed)]);
    const qd = stepPath(qpts, W - right);
    el("path", { d: `${qd}V${qy0}H${qpts[0][0]}Z`, fill: "var(--bar)", opacity: 0.7 }, svg);
    for (const [k, , c] of SERIES) {
      const sp = pts.filter((p) => p[k] != null).map((p) => [x(p.t), y(p[k])]);
      el("path", { d: stepPath(sp, W - right), fill: "none", stroke: c, "stroke-width": 2, "stroke-linejoin": "round" }, svg);
    }
    // crosshair
    const cross = el("line", { y1: top, y2: qy0, stroke: "var(--muted)", "stroke-width": 1, visibility: "hidden" }, svg);
    const dots = SERIES.map(([, , c]) => el("circle", { r: 4, fill: c, stroke: "var(--card)", "stroke-width": 2, visibility: "hidden" }, svg));
    const hit = el("rect", { x: left, y: top, width: W - left - right, height: qy0 - top, fill: "transparent" }, svg);
    hit.addEventListener("mousemove", (e) => {
      const r = svg.getBoundingClientRect();
      const px = (e.clientX - r.left) * (W / r.width);
      const t = t0 + ((px - left) / (W - left - right)) * (t1 - t0);
      let i = 0;
      while (i + 1 < pts.length && pts[i + 1].t <= t) i++;
      const p = pts[i];
      cross.setAttribute("x1", px); cross.setAttribute("x2", px); cross.setAttribute("visibility", "visible");
      SERIES.forEach(([k], j) => {
        if (p[k] == null) return dots[j].setAttribute("visibility", "hidden");
        dots[j].setAttribute("cx", px); dots[j].setAttribute("cy", y(p[k])); dots[j].setAttribute("visibility", "visible");
      });
      const rows = SERIES.map(([k, label, c]) => `<tr><td><span class="k" style="background:${c}"></span>${label}</td><td><b>${money(p[k])}</b></td></tr>`).join("");
      showTip(`<div class="t">${esc(when(t))}</div><table>${rows}<tr><td>Listed</td><td>${p.listed.toLocaleString()}</td></tr>` +
              `${p.carried ? "" : `<tr><td>Sold since prior</td><td>${p.sold}</td></tr>`}</table>` +
              `<div class="t">since ${esc(when(p.t))}${p.carried ? " (before window)" : ""}</div>`, e.clientX, e.clientY);
    });
    hit.addEventListener("mouseleave", () => {
      cross.setAttribute("visibility", "hidden");
      dots.forEach((d) => d.setAttribute("visibility", "hidden"));
      hideTip();
    });
  });

  // ---- flips price index: UTC [weekday][hour] ratios, rotated into local time; diverging blue (cheap) / red (dear)
  document.querySelectorAll(".pindex").forEach((box) => {
    const utc = JSON.parse(box.dataset.index).flat();
    const offset = Math.round(-new Date().getTimezoneOffset() / 60);
    const local = Array.from({ length: 168 }, (_, i) => utc[((i - offset) % 168 + 168) % 168]);
    const devs = local.filter((v) => v != null).map((v) => v - 1);
    if (!devs.length) { box.innerHTML = `<p class="muted small">Not enough snapshots yet.</p>`; return; }
    const m = Math.max(0.005, ...devs.map(Math.abs));
    const pct = (v) => `${v >= 1 ? "+" : ""}${((v - 1) * 100).toFixed(1)}%`;
    const fill = (v) => {
      if (v == null) return "var(--grid)";
      const f = Math.min(1, Math.abs(v - 1) / m);
      return `color-mix(in oklab, ${v < 1 ? "var(--div-neg)" : "var(--div-pos)"} ${(f * 100).toFixed(0)}%, var(--div-mid))`;
    };
    let html = `<table class="heat"><tr><th></th>`;
    for (let h = 0; h < 24; h++) html += `<th>${h % 3 === 0 ? h : ""}</th>`;
    html += `<th></th></tr>`;
    for (let d = 0; d < 7; d++) {
      const row = local.slice(d * 24, d * 24 + 24);
      const vs = row.filter((v) => v != null);
      const avg = vs.length ? vs.reduce((a, b) => a + b, 0) / vs.length : null;
      html += `<tr><td class="day">${DAYS[d]}</td>`;
      row.forEach((v, h) => {
        html += `<td class="cell" data-tip="${v == null ? "" : `${DAYS[d]} ${String(h).padStart(2, "0")}:00 · ${pct(v)} vs median`}" style="background:${fill(v)}"></td>`;
      });
      html += `<td class="best">${avg == null ? "" : `avg ${pct(avg)}`}</td></tr>`;
    }
    html += `</table>`;
    const cells = local.map((v, i) => [v, i]).filter(([v]) => v != null);
    const [lo, li] = cells.reduce((a, b) => (b[0] < a[0] ? b : a));
    const [hi, hi_i] = cells.reduce((a, b) => (b[0] > a[0] ? b : a));
    const slot = (i) => `${DAYS[Math.floor(i / 24)]} ${String(i % 24).padStart(2, "0")}h`;
    html += `<div class="scale"><span>−${(m * 100).toFixed(1)}%</span><span class="ramp"></span><span>+${(m * 100).toFixed(1)}%</span>
      <span>· Local time (UTC${offset >= 0 ? "+" : ""}${offset}). Cheapest <b>${slot(li)}</b> (${pct(lo)}), dearest <b>${slot(hi_i)}</b> (${pct(hi)}):
      swing ${((hi / lo - 1) * 100).toFixed(1)}% vs the 5% AH cut.</span></div>`;
    box.innerHTML = html;
    box.addEventListener("mousemove", (e) => {
      const t = e.target.closest("td.cell");
      if (t && t.dataset.tip) showTip(esc(t.dataset.tip), e.clientX, e.clientY); else hideTip();
    });
    box.addEventListener("mouseleave", hideTip);
  });
})();
