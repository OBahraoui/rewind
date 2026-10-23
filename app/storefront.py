"""The demo shop's storefront: live prices for sample baskets, plus what's serving them.

Every price on the page comes from this revision's own POST /orders/quote, so what you
see is exactly what production charges. When pricing breaks, the page says so plainly.
"""

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Northbeam Coffee Supply · Rewind demo shop</title>
<meta name="description" content="Demo shop for Rewind: every price is quoted live by the revision serving production.">
<style>
:root {
  color-scheme: light;
  --bg: #f7f2ea; --card: #fffdf9; --ink: #2a1d14; --ink-2: #5e4b3d; --ink-3: #8f7b6b; --line: #e8dfd2;
  --brand: #7a4528; --brand-ink: #fff; --ribbon: #1e1813; --ribbon-ink: #efe6db;
  --ok: #0ca30c; --ok-tint: #e6f4e2; --bad: #d03b3b; --bad-tint: #fbe4e1; --bad-ink: #9e2323;
  font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
@media (prefers-color-scheme: dark) {
  :root { color-scheme: dark; --bg: #15110d; --card: #1e1915; --ink: #f3ebe1; --ink-2: #cdbfb1; --ink-3: #998877;
    --line: #2f2822; --brand: #d0905f; --brand-ink: #1a120c; --ribbon: #0b0907; --ok-tint: #15301a;
    --bad-tint: #3a1918; --bad-ink: #ff9b8f; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); line-height: 1.5; }
.ribbon { background: var(--ribbon); color: var(--ribbon-ink); font-size: .8rem; }
.ribbon .in { max-width: 1080px; margin: 0 auto; padding: 8px 20px; display: flex; gap: 8px 22px; flex-wrap: wrap;
  align-items: center; }
.ribbon b { color: #fff; font-weight: 600; }
.ribbon .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: var(--ok); margin-right: 6px; }
.dot.bad { background: var(--bad); }
.pill { padding: 1px 8px; border-radius: 99px; background: rgb(255 255 255 / 10%); }
.pill.on { background: #5b4bdb; color: #fff; }
header.brand { max-width: 1080px; margin: 0 auto; padding: 28px 20px 8px; display: flex; align-items: center; gap: 14px; }
.logo { width: 42px; height: 42px; border-radius: 12px; background: var(--brand); display: grid; place-items: center; }
.brand h1 { margin: 0; font-family: ui-serif, Georgia, "Times New Roman", serif; font-size: 1.6rem; letter-spacing: -.01em; }
.brand p { margin: 0; color: var(--ink-3); font-size: .9rem; }
main { max-width: 1080px; margin: 0 auto; padding: 12px 20px 48px; display: grid; gap: 28px; }
h2 { font-size: .8rem; letter-spacing: .1em; text-transform: uppercase; color: var(--ink-3); margin: 0 0 12px; font-weight: 600; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 14px; }
.product { background: var(--card); border: 1px solid var(--line); border-radius: 16px; padding: 16px; display: grid; gap: 10px; }
.product .art { height: 84px; border-radius: 12px; background: color-mix(in srgb, var(--brand) 10%, var(--card));
  display: grid; place-items: center; color: var(--brand); }
.product .name { font-weight: 600; }
.product .price { color: var(--ink-2); font-variant-numeric: tabular-nums; }
.panel { background: var(--card); border: 1px solid var(--line); border-radius: 18px; overflow: hidden; }
.panel.bad { border-color: var(--bad); box-shadow: 0 0 0 4px var(--bad-tint); }
.verdict { display: flex; gap: 12px; align-items: center; padding: 16px 20px; background: var(--ok-tint); font-weight: 600; }
.verdict .big { font-size: 1.05rem; }
.verdict small { display: block; font-weight: 400; color: var(--ink-2); }
.panel.bad .verdict { background: var(--bad-tint); color: var(--bad-ink); }
.verdict .icon { width: 30px; height: 30px; border-radius: 50%; display: grid; place-items: center; color: #fff;
  background: var(--ok); font-size: 1rem; flex: none; }
.panel.bad .verdict .icon { background: var(--bad); }
table { width: 100%; border-collapse: collapse; }
th { text-align: left; font-size: .72rem; letter-spacing: .08em; text-transform: uppercase; color: var(--ink-3);
  padding: 12px 20px; border-bottom: 1px solid var(--line); font-weight: 600; }
td { padding: 14px 20px; border-bottom: 1px solid var(--line); vertical-align: middle; }
tr:last-child td { border-bottom: 0; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
td.total { font-size: 1.6rem; font-weight: 700; letter-spacing: -.02em; }
td.total.bad { color: var(--bad); }
.items { font-size: .82rem; color: var(--ink-3); }
.tag { font-size: .75rem; padding: 2px 8px; border-radius: 99px; border: 1px solid var(--line); color: var(--ink-2); white-space: nowrap; }
.tag.new { border-color: #5b4bdb; color: #5b4bdb; }
.status { font-size: .8rem; font-weight: 600; white-space: nowrap; }
.status.ok { color: var(--ok); } .status.bad { color: var(--bad); }
footer { max-width: 1080px; margin: 0 auto; padding: 0 20px 40px; color: var(--ink-3); font-size: .8rem; }
.flash { animation: flash .6s ease-out; }
@keyframes flash { from { background: color-mix(in srgb, var(--bad) 22%, transparent); } }
@media (prefers-reduced-motion: reduce) { .flash { animation: none; } }
@media (max-width: 640px) {
  .hide-sm { display: none; }
  td, th { padding: 12px 14px; }
  td.total { font-size: 1.25rem; }
}
</style>
</head>
<body>
<div class="ribbon" role="status" aria-live="polite">
  <div class="in" id="ribbon"><span><span class="dot"></span>Loading production status…</span></div>
</div>
<header class="brand">
  <div class="logo" aria-hidden="true">
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round">
      <path d="M4 9h12v5a5 5 0 0 1-5 5H9a5 5 0 0 1-5-5z"/><path d="M16 10h1.5a2.5 2.5 0 0 1 0 5H16"/><path d="M8 3c0 1.5 1 1.5 1 3M12 3c0 1.5 1 1.5 1 3"/>
    </svg>
  </div>
  <div><h1>Northbeam Coffee Supply</h1><p>Small-batch coffee and tea, shipped from Rewind's demo production.</p></div>
</header>
<main>
  <section aria-labelledby="price-h">
    <h2 id="price-h">What production charges right now</h2>
    <div class="panel" id="panel">
      <div class="verdict" id="verdict"><span class="icon" aria-hidden="true">✓</span><span class="big">Checking prices…</span></div>
      <table>
        <thead><tr><th scope="col">Basket</th><th scope="col" class="num hide-sm">Subtotal</th>
          <th scope="col" class="num">Charged</th><th scope="col" class="hide-sm">Pricing</th><th scope="col">Check</th></tr></thead>
        <tbody id="quotes"></tbody>
      </table>
    </div>
  </section>
  <section aria-labelledby="prod-h">
    <h2 id="prod-h">The range</h2>
    <div class="grid" id="products"></div>
  </section>
</main>
<footer>Rewind demo shop. Refreshes every 2 seconds; every price comes from <code>POST /orders/quote</code> on the
revision shown above. A total below half the subtotal means the margin guardrail is broken, and production monitoring
will catch it.</footer>
<script>
"use strict";
const BASKETS = [
  ["One tea", [["tea", 1]]],
  ["Mug and beans", [["mug", 1], ["beans", 1]]],
  ["Barista kit", [["grinder", 1], ["kettle", 1], ["beans", 2]]],
  ["Bulk tea (20)", [["tea", 20]]],
];
const ART = {
  tea: '<path d="M7 10h10v3a5 5 0 0 1-10 0z"/><path d="M12 4c2 2 0 3 2 5"/>',
  mug: '<path d="M5 8h11v7a4 4 0 0 1-4 4H9a4 4 0 0 1-4-4z"/><path d="M16 10h1.5a2.5 2.5 0 0 1 0 5H16"/>',
  beans: '<ellipse cx="9" cy="12" rx="3.5" ry="5"/><path d="M9 7c-1 3 1 7 0 10"/><ellipse cx="16" cy="12" rx="3.5" ry="5"/><path d="M16 7c-1 3 1 7 0 10"/>',
  kettle: '<path d="M6 11h10l1 7H5z"/><path d="M8 11a3 3 0 0 1 6 0"/><path d="M16 13l4-3"/>',
  grinder: '<path d="M7 9h10v10H7z"/><path d="M9 9V6h6v3"/><path d="M12 3v3"/><circle cx="12" cy="14" r="2"/>',
};
const money = (c) => (c < 0 ? "−$" : "$") + (Math.abs(c) / 100).toFixed(2);
const esc = (s) => String(s).replace(/[&<>"]/g, (ch) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[ch]));
let lastBroken = null;
async function refresh() {
  try {
    const health = await (await fetch("health", {cache: "no-store"})).json();
    const flags = Object.entries(health.flags || {});
    const flagHtml = flags.length ? flags.map(([k, v]) => `<span class="pill${v ? " on" : ""}">${esc(k)}: ${v ? "on" : "off"}</span>`).join(" ")
      : '<span class="pill">no flags on</span>';
    const flagsOk = (health.flags_status || "ok") === "ok" || health.flags_status === "unconfigured";
    document.getElementById("ribbon").innerHTML =
      `<span><span class="dot${flagsOk ? "" : " bad"}"></span><b>${esc(health.environment)}</b></span>` +
      `<span>revision <span class="mono">${esc(health.revision)}</span></span>` +
      `<span>version <span class="mono">${esc(health.version)}</span></span><span>flags ${flagHtml}</span>`;
    const products = await (await fetch("products", {cache: "no-store"})).json();
    document.getElementById("products").innerHTML = Object.entries(products).map(([sku, p]) =>
      `<div class="product"><div class="art" aria-hidden="true"><svg width="44" height="44" viewBox="0 0 24 24" fill="none"
        stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${ART[sku] || ""}</svg></div>
        <div class="name">${esc(p.name)}</div><div class="price">${money(p.price_cents)}</div></div>`).join("");
    const quotes = await Promise.all(BASKETS.map(async ([name, items]) => {
      const r = await fetch("orders/quote", {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({customer_id: "storefront", items: items.map(([sku, quantity]) => ({sku, quantity}))})});
      return [name, items, await r.json()];
    }));
    let worst = null;
    document.getElementById("quotes").innerHTML = quotes.map(([name, items, q]) => {
      const bad = q.total_cents < q.subtotal_cents / 2;
      if (bad && (!worst || q.total_cents - q.subtotal_cents < worst.total_cents - worst.subtotal_cents)) worst = q;
      const list = items.map(([sku, n]) => `${n} × ${esc((products[sku] || {}).name || sku)}`).join(", ");
      return `<tr><td><div>${esc(name)}</div><div class="items">${list}</div></td>
        <td class="num hide-sm">${money(q.subtotal_cents)}</td>
        <td class="num total${bad ? " bad" : ""}">${money(q.total_cents)}</td>
        <td class="hide-sm"><span class="tag${q.pricing === "new" ? " new" : ""}">${q.pricing === "new" ? "new pricing" : "standard"}</span></td>
        <td><span class="status ${bad ? "bad" : "ok"}">${bad ? "✕ below cost" : "✓ ok"}</span></td></tr>`;
    }).join("");
    const panel = document.getElementById("panel");
    const verdict = document.getElementById("verdict");
    panel.classList.toggle("bad", !!worst);
    if (worst) {
      verdict.innerHTML = `<span class="icon" aria-hidden="true">✕</span><span class="big">A ${money(worst.subtotal_cents)} order is being charged ${money(worst.total_cents)}
        <small>Production is pricing below cost. Monitoring will catch this and Rewind will undo the release.</small></span>`;
    } else {
      verdict.innerHTML = `<span class="icon" aria-hidden="true">✓</span><span class="big">Prices are healthy
        <small>Every basket is within the margin guardrail.</small></span>`;
    }
    if (lastBroken !== null && lastBroken !== !!worst) { panel.classList.remove("flash"); void panel.offsetWidth; panel.classList.add("flash"); }
    lastBroken = !!worst;
  } catch (e) {
    document.getElementById("ribbon").innerHTML = `<span><span class="dot bad"></span>Production unreachable: ${esc(e.message || e)}</span>`;
  }
}
refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""
