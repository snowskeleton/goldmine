// Hour-of-week heatmap. The server sends UTC buckets (index = weekday*24 + hour, 0 = Sunday 00h);
// they're rotated into the browser's local time here.
(async function () {
  const root = document.getElementById("timing");
  if (!root || !root.dataset.item) return;
  const res = await fetch(`/web/timing?item=${encodeURIComponent(root.dataset.item)}`, { credentials: "same-origin" });
  const data = await res.json();
  if (!res.ok) { root.innerHTML = `<p class="error">${data.error || "error"}</p>`; return; }
  const offset = Math.round(-new Date().getTimezoneOffset() / 60);  // whole hours; buckets are hourly
  const days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const money = (c) => {
    if (c == null) return "—";
    const g = Math.floor(c / 10000), s = Math.floor(c / 100) % 100, cu = c % 100;
    return g ? `${g.toLocaleString()}g ${s}s` : s ? `${s}s ${cu}c` : `${cu}c`;
  };
  const esc = (t) => String(t).replace(/[&<>"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]));
  let html = `<h2>${esc(data.name)} (${data.item_id})</h2>`;
  if (!data.markets.length) html += `<p class="muted">No data for this item yet.</p>`;
  for (const m of data.markets) {
    const local = Array.from({ length: 168 }, (_, i) => m.buckets[((i - offset) % 168 + 168) % 168]);
    const peak = Math.max(0.0001, ...local.map((b) => b.sold_avg || 0));
    const peakQty = Math.max(1, ...local.map((b) => b.qty_avg || 0));
    html += `<h3>${esc(m.market)} <span class="muted small">${esc(m.game || "")}</span></h3>`;
    for (const [title, value, max, fmt] of [
      ["Units sold per hour", (b) => b.sold_avg || 0, peak, (v) => v.toFixed(1)],
      ["Units listed", (b) => b.qty_avg || 0, peakQty, (v) => Math.round(v).toLocaleString()],
    ]) {
      html += `<p class="small"><strong>${title}</strong></p><table class="heat"><tr><th></th>`;
      for (let h = 0; h < 24; h++) html += `<th>${h % 3 === 0 ? h : ""}</th>`;
      html += `<th></th></tr>`;
      for (let d = 0; d < 7; d++) {
        html += `<tr><td class="day">${days[d]}</td>`;
        const row = local.slice(d * 24, d * 24 + 24);
        row.forEach((b, h) => {
          const v = value(b);
          const tip = b.samples
            ? `${days[d]} ${h}:00 · ${(b.sold_avg || 0).toFixed(1)} sold/hr · ${Math.round(b.qty_avg || 0)} listed · typical ${money(b.p10_avg)} · ${b.samples} samples`
            : `${days[d]} ${h}:00 · no data`;
          const alpha = b.samples ? 0.12 + 0.88 * (v / max) : 0.04;
          html += `<td class="cell" title="${esc(tip)}" style="background: rgba(var(--heat), ${alpha.toFixed(3)})"></td>`;
        });
        const best = row.map((b, h) => [value(b), h]).filter(([v]) => v > 0).sort((a, b) => b[0] - a[0]).slice(0, 3);
        html += `<td class="best">${best.map(([v, h]) => `${String(h).padStart(2, "0")}h (${fmt(v)})`).join(", ")}</td></tr>`;
      }
      html += `</table>`;
    }
    html += `<p class="legend">Local time (UTC${offset >= 0 ? "+" : ""}${offset}). Darker = more. Hover a cell for details.</p>`;
  }
  root.innerHTML = html;
})();
