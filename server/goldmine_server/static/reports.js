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
    ths.forEach((th) => {
      if (!th.dataset.sort) return;
      th.tabIndex = 0;
      const sort = () => {
        const cur = th.getAttribute("aria-sort");  // numbers start high-to-low, text A-Z
        const desc = cur ? cur === "ascending" : th.dataset.sort === "num";
        ths.forEach((h) => h.removeAttribute("aria-sort"));
        th.setAttribute("aria-sort", desc ? "descending" : "ascending");
        const body = table.tBodies[0], col = th.cellIndex;  // columns can be reordered
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
      th.addEventListener("click", () => { if (!table.dataset.busy) sort(); });
      th.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); sort(); } });
    });
  });

  // ---- resizable, movable columns: drag a header's right edge to resize it (double-click the edge to undo),
  // drag the header itself to move the column. Saved in a cookie per table shape, so every section of a
  // report shares one layout.
  const colKey = (th) => th.textContent.replace(/\d+/g, "").trim().toLowerCase();  // "Last 72h" == "Last 24h"
  const heads = (table) => [...table.tHead.rows[0].cells];
  const layouts = new Map();
  let dragging = null;

  function cookieName(keys) {
    let h = 0;
    for (const ch of [...keys].sort().join("|")) h = (h * 31 + ch.charCodeAt(0)) | 0;
    return "gm_cols_" + (h >>> 0).toString(36);
  }
  function loadLayout(name) {
    const m = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`));
    try { const v = m && JSON.parse(decodeURIComponent(m[1])); if (v) return { o: v.o || [], w: v.w || {} }; } catch (e) { /* bad cookie */ }
    return { o: [], w: {} };
  }
  const custom = (L) => L.o.length > 0 || Object.keys(L.w).length > 0;
  function saveLayout(L) {
    const v = custom(L) ? encodeURIComponent(JSON.stringify({ o: L.o, w: L.w })) : "";
    document.cookie = `${L.name}=${v}; path=/; max-age=${custom(L) ? 31536000 : 0}; SameSite=Lax`;
    L.resets.forEach((b) => { b.hidden = !custom(L); });
  }

  // Put the columns in L.o's order (columns it doesn't name keep their place at the end).
  function arrange(table, order) {
    const keys = heads(table).map(colKey), n = keys.length;
    const idx = order.map((k) => keys.indexOf(k)).filter((i) => i >= 0);
    keys.forEach((k, i) => { if (!idx.includes(i)) idx.push(i); });
    if (idx.every((c, i) => c === i)) return;
    [...table.rows].forEach((tr) => {
      if (tr.cells.length !== n) return;
      const cells = [...tr.cells];
      idx.forEach((i) => tr.appendChild(cells[i]));
    });
  }

  // Fixed layout from the saved widths; columns without one keep their natural width.
  function size(table, w) {
    const ths = heads(table);
    table.classList.remove("sized");
    table.style.width = "";
    ths.forEach((th) => { th.style.width = ""; });
    if (!ths.some((th) => w[colKey(th)]) || !table.offsetParent) return;  // hidden in a closed <details>: sized on open
    const natural = ths.map((th) => Math.ceil(th.getBoundingClientRect().width));
    let total = 0;
    ths.forEach((th, i) => { const px = w[colKey(th)] || natural[i]; th.style.width = `${px}px`; total += px; });
    table.style.width = `${total}px`;
    table.classList.add("sized");
  }
  function widen(table, key, px) {
    if (!table.classList.contains("sized")) return;
    let total = 0;
    heads(table).forEach((th) => {
      if (colKey(th) === key) th.style.width = `${px}px`;
      total += parseFloat(th.style.width) || 0;
    });
    table.style.width = `${total}px`;
  }

  document.querySelectorAll("table.sortable").forEach((table) => {
    const name = cookieName(heads(table).map(colKey));
    if (!layouts.has(name)) layouts.set(name, { name, ...loadLayout(name), tables: [], resets: [] });
    const L = layouts.get(name);
    L.tables.push(table);
    table.dataset.cols = heads(table).map(colKey).join("\n");  // the template's order, for Reset
    arrange(table, L.o);
    size(table, L.w);

    heads(table).forEach((th) => {
      const g = document.createElement("span");
      g.className = "colgrip";
      th.appendChild(g);
      g.addEventListener("pointerdown", (e) => {
        e.preventDefault();
        e.stopPropagation();
        table.dataset.busy = "1";  // no sort on the click and no column drag that follow
        const key = colKey(th), x0 = e.clientX, w0 = Math.round(th.getBoundingClientRect().width);
        if (!L.w[key]) { L.w[key] = w0; L.tables.forEach((t) => size(t, L.w)); }
        g.setPointerCapture(e.pointerId);
        g.classList.add("on");
        const move = (ev) => {
          L.w[key] = Math.max(36, w0 + Math.round(ev.clientX - x0));
          L.tables.forEach((t) => widen(t, key, L.w[key]));
        };
        const up = () => {
          g.removeEventListener("pointermove", move);
          g.removeEventListener("pointerup", up);
          g.classList.remove("on");
          saveLayout(L);
          setTimeout(() => { delete table.dataset.busy; }, 0);
        };
        g.addEventListener("pointermove", move);
        g.addEventListener("pointerup", up);
      });
      g.addEventListener("dblclick", (e) => {
        e.stopPropagation();
        delete L.w[colKey(th)];
        L.tables.forEach((t) => size(t, L.w));
        saveLayout(L);
      });

      th.draggable = true;
      const unmark = () => th.classList.remove("drop-l", "drop-r");
      th.addEventListener("dragstart", (e) => {
        if (table.dataset.busy) return e.preventDefault();
        dragging = { L, key: colKey(th) };
        e.dataTransfer.effectAllowed = "move";
        e.dataTransfer.setData("text/plain", dragging.key);
        th.classList.add("dragging");
      });
      th.addEventListener("dragend", () => { th.classList.remove("dragging"); dragging = null; });
      th.addEventListener("dragover", (e) => {
        if (!dragging || dragging.L !== L || dragging.key === colKey(th)) return;
        e.preventDefault();
        const r = th.getBoundingClientRect(), after = e.clientX > r.left + r.width / 2;
        th.classList.toggle("drop-l", !after);
        th.classList.toggle("drop-r", after);
      });
      th.addEventListener("dragleave", unmark);
      th.addEventListener("drop", (e) => {
        e.preventDefault();
        const after = th.classList.contains("drop-r");
        unmark();
        if (!dragging || dragging.L !== L) return;
        const order = heads(table).map(colKey).filter((k) => k !== dragging.key);
        order.splice(order.indexOf(colKey(th)) + (after ? 1 : 0), 0, dragging.key);
        L.o = order;
        L.tables.forEach((t) => arrange(t, order));
        saveLayout(L);
      });
    });

    const head = table.closest("section")?.querySelector(":scope > header.sect");
    if (head && !head.querySelector(".colreset")) {
      const b = document.createElement("button");
      b.className = "link small colreset";
      b.textContent = "Reset columns";
      b.title = "Undo column sizes and order for this report";
      b.addEventListener("click", () => {
        L.o = [];
        L.w = {};
        L.tables.forEach((t) => {
          const keys = heads(t).map(colKey);
          arrange(t, t.dataset.cols.split("\n").filter((k) => keys.includes(k)));
          size(t, L.w);
        });
        saveLayout(L);
      });
      b.hidden = !custom(L);
      head.appendChild(b);
      L.resets.push(b);
    }
  });
  document.querySelectorAll("details").forEach((d) => d.addEventListener("toggle", () => {
    if (d.open) d.querySelectorAll("table.sortable").forEach((t) => size(t, layouts.get(cookieName(heads(t).map(colKey))).w));
  }));

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

  // ---- price history: min / p10 / median step lines, units listed below (its own scale), crosshair.
  // Realm charts can add overlays (data-overlays): the region baseline sized like this realm, the
  // plain region median, another realm. Click a legend entry to show or hide its line. When the
  // lines are far apart (one 50x the other), the price axis goes logarithmic so both stay readable.
  // An item's charts (one per market and variant) share their price and time axes so they compare at
  // a glance; toggling a line redraws the whole group.
  const SERIES = [["min", "Lowest", "var(--s1)"], ["p10", "P10", "var(--s2)"], ["median", "Median", "var(--s3)"]];
  const OVERLAY_STYLE = {
    region: { c: "var(--s4)", w: 2.5, dash: "", on: true },
    region_raw: { c: "var(--s4)", w: 1.5, dash: "2 3", on: false },
    compare: { c: "var(--s5)", w: 2, dash: "6 4", on: true },
  };
  function niceTicks(lo, hi, n) {
    const span = hi - lo || Math.abs(hi) || 1;
    const step0 = span / n, mag = 10 ** Math.floor(Math.log10(step0));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= step0);
    const out = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(v);
    return out;
  }
  function logTicks(lo, hi) {
    const out = [], mults = Math.log10(hi / lo) > 3 ? [1] : [1, 2, 5];
    for (let e = Math.floor(Math.log10(lo)); 10 ** e <= hi; e++)
      for (const m of mults) { const v = m * 10 ** e; if (v >= lo && v <= hi) out.push(v); }
    return out;
  }
  const pgroups = new Map();
  document.querySelectorAll(".pchart").forEach((box) => {
    const pts = JSON.parse(box.dataset.points);
    if (!pts.length) return;
    const gkey = box.closest(".report") || box;
    if (!pgroups.has(gkey)) pgroups.set(gkey, []);
    const group = pgroups.get(gkey);
    const lines = SERIES.map(([k, label, c]) => ({ k, label, c, w: 2, dash: "", on: true }))
      .concat(JSON.parse(box.dataset.overlays || "[]").map(([k, label]) => ({ k, label, ...OVERLAY_STYLE[k] })));
    const legend = document.createElement("div");
    legend.className = "legend-row";
    box.before(legend);
    const drawLegend = () => {
      legend.innerHTML = lines.map((l, i) => `<button type="button" class="lg${l.on ? "" : " off"}" data-i="${i}">` +
        `<span class="sw" style="background:${l.c}${l.dash ? ";opacity:.6" : ""}"></span>${esc(l.label)}</button>`).join("") +
        `<span><span class="sw" style="background:var(--bar);height:8px"></span>Units listed</span>`;
    };
    legend.addEventListener("click", (e) => {
      const b = e.target.closest("button.lg");
      if (!b) return;
      lines[+b.dataset.i].on = !lines[+b.dataset.i].on;
      drawLegend();
      group.forEach((g) => g.draw());
    });
    drawLegend();
    const member = {
      t0: pts[0].t, t1: pts[pts.length - 1].t + 1,
      vals: () => pts.flatMap((p) => lines.filter((l) => l.on).map((l) => p[l.k]).filter((v) => v != null && v > 0)),
      draw,
    };
    group.push(member);
    function draw() {
      box.replaceChildren();
      const shown = lines.filter((l) => l.on);
      const W = Math.max(320, box.clientWidth), H = 260, top = 8, priceH = 170, gap = 18, qtyH = 50, left = 64, right = 12;
      const tNow = Date.now() / 1000;
      const t0 = Math.min(...group.map((g) => g.t0)), t1 = Math.max(tNow, ...group.map((g) => g.t1));
      const vals = group.flatMap((g) => g.vals());
      if (!vals.length) vals.push(1);
      let lo = Math.min(...vals), hi = Math.max(...vals);
      const logScale = hi / lo > 20;
      let y, ticks;
      if (logScale) {
        lo /= 1.15; hi *= 1.15;
        const L0 = Math.log(lo), L1 = Math.log(hi);
        y = (v) => top + (1 - (Math.log(Math.max(v, lo)) - L0) / (L1 - L0)) * priceH;
        ticks = logTicks(lo, hi);
      } else {
        const padv = (hi - lo) * 0.08 || hi * 0.05 || 1;
        lo = Math.max(0, lo - padv); hi += padv;
        y = (v) => top + (1 - (v - lo) / (hi - lo)) * priceH;
        ticks = niceTicks(lo, hi, 4);
      }
      const qmax = Math.max(1, ...pts.map((p) => p.listed));
      const x = (t) => left + ((t - t0) / (t1 - t0)) * (W - left - right);
      const qy0 = top + priceH + gap + qtyH, qy = (q) => qy0 - (q / qmax) * qtyH;
      const svg = el("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Price history chart" }, box);
      const axis = el("g", { class: "axis" }, svg);
      for (const v of ticks) {
        el("line", { x1: left, x2: W - right, y1: y(v), y2: y(v) }, axis);
        el("text", { x: left - 8, y: y(v) + 4, "text-anchor": "end" }, axis).textContent = money(Math.round(v));
      }
      if (logScale) el("text", { x: W - right, y: top + 10, "text-anchor": "end" }, axis).textContent = "log scale";
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
      for (const l of shown) {
        const sp = pts.filter((p) => p[l.k] != null && p[l.k] > 0).map((p) => [x(p.t), y(p[l.k])]);
        el("path", { d: stepPath(sp, W - right), fill: "none", stroke: l.c, "stroke-width": l.w, "stroke-linejoin": "round",
                     "stroke-dasharray": l.dash }, svg);
      }
      // crosshair
      const cross = el("line", { y1: top, y2: qy0, stroke: "var(--muted)", "stroke-width": 1, visibility: "hidden" }, svg);
      const dots = shown.map((l) => el("circle", { r: 4, fill: l.c, stroke: "var(--card)", "stroke-width": 2, visibility: "hidden" }, svg));
      const hit = el("rect", { x: left, y: top, width: W - left - right, height: qy0 - top, fill: "transparent" }, svg);
      hit.addEventListener("mousemove", (e) => {
        const r = svg.getBoundingClientRect();
        const px = (e.clientX - r.left) * (W / r.width);
        const t = t0 + ((px - left) / (W - left - right)) * (t1 - t0);
        if (t < pts[0].t) {  // before this variant was first seen (the group's axis starts earlier)
          cross.setAttribute("visibility", "hidden");
          dots.forEach((d) => d.setAttribute("visibility", "hidden"));
          return hideTip();
        }
        let i = 0;
        while (i + 1 < pts.length && pts[i + 1].t <= t) i++;
        const p = pts[i];
        cross.setAttribute("x1", px); cross.setAttribute("x2", px); cross.setAttribute("visibility", "visible");
        shown.forEach((l, j) => {
          if (p[l.k] == null) return dots[j].setAttribute("visibility", "hidden");
          dots[j].setAttribute("cx", px); dots[j].setAttribute("cy", y(p[l.k])); dots[j].setAttribute("visibility", "visible");
        });
        const extra = (k) => k === "compare" && p.compare_raw != null ? ` <span class="muted">(listed ${money(p.compare_raw)})</span>`
          : k === "region" && p.realms ? ` <span class="muted">(${p.realms} realms)</span>` : "";
        const rows = lines.filter((l) => p[l.k] != null).map((l) =>
          `<tr><td><span class="k" style="background:${l.c}"></span>${esc(l.label)}</td><td><b>${money(p[l.k])}</b>${extra(l.k)}</td></tr>`).join("");
        showTip(`<div class="t">${esc(when(t))}</div><table>${rows}<tr><td>Listed</td><td>${p.listed.toLocaleString()}</td></tr>` +
                `${p.carried ? "" : `<tr><td>Sold since prior</td><td>${p.sold}</td></tr>`}</table>` +
                `<div class="t">since ${esc(when(p.t))}${p.carried ? " (before window)" : ""}</div>`, e.clientX, e.clientY);
      });
      hit.addEventListener("mouseleave", () => {
        cross.setAttribute("visibility", "hidden");
        dots.forEach((d) => d.setAttribute("visibility", "hidden"));
        hideTip();
      });
    }
  });
  pgroups.forEach((group) => group.forEach((g) => g.draw()));

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
