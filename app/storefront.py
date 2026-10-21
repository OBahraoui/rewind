"""A one-page storefront: live prices for sample baskets, the serving revision and flags.

It only calls this service's own API, so what you see is exactly what production charges.
"""

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rewind demo shop</title>
<style>
:root { --bg:#fbfaf8; --card:#fff; --ink:#1d1c1a; --muted:#6b6862; --line:#e6e3de; --ok:#1f8a5b; --bad:#c0392b;
        --accent:#5b4bdb; font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }
@media (prefers-color-scheme: dark) { :root { --bg:#141413; --card:#1d1c1a; --ink:#f0eee9; --muted:#a29e96;
        --line:#2f2d2a; --ok:#5fd39b; --bad:#ff8a7a; --accent:#a99cff; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink); }
header { padding:1rem 16px; border-bottom:1px solid var(--line); background:var(--card); display:flex; gap:1rem;
         align-items:baseline; flex-wrap:wrap; }
header h1 { font-size:1.2rem; margin:0; }
.status { color:var(--muted); font-size:.85rem; }
.status b { color:var(--ink); }
.flag-on { color:var(--accent); font-weight:700; }
main { max-width:960px; margin:0 auto; padding:1.5rem 16px 3rem; }
h2 { font-size:1rem; margin:1.6rem 0 .8rem; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }
.grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(170px, 1fr)); gap:.8rem; }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:1rem; }
.card .name { font-weight:600; } .card .price { color:var(--muted); margin-top:.3rem; }
table { width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line); border-radius:12px;
        overflow:hidden; }
th, td { padding:.75rem 1rem; text-align:left; border-bottom:1px solid var(--line); }
th { font-size:.8rem; color:var(--muted); text-transform:uppercase; }
td.total { font-size:1.5rem; font-weight:800; font-variant-numeric:tabular-nums; }
.ok { color:var(--ok); } .bad { color:var(--bad); }
.warn { margin-top:1rem; padding:.8rem 1rem; border-radius:10px; border:1px solid var(--bad); color:var(--bad);
        font-weight:600; display:none; }
footer { color:var(--muted); font-size:.8rem; text-align:center; padding:2rem 16px; }
</style>
</head>
<body>
<header>
  <h1>☕ Rewind demo shop</h1>
  <span class="status" id="status">loading…</span>
</header>
<main>
  <h2>Products</h2>
  <div class="grid" id="products"></div>
  <h2>What production charges right now</h2>
  <table>
    <thead><tr><th>Basket</th><th>Subtotal</th><th>Total charged</th><th>Pricing</th></tr></thead>
    <tbody id="quotes"></tbody>
  </table>
  <div class="warn" id="warn">⚠ Some baskets are priced below cost. Production monitoring will catch this.</div>
</main>
<footer>Live: refreshes every 2 seconds. Every price comes from
<code>POST /orders/quote</code> on this revision.</footer>
<script>
const BASKETS = [
  ["One tea", [["tea", 1]]],
  ["Mug and beans", [["mug", 1], ["beans", 1]]],
  ["Barista kit", [["grinder", 1], ["kettle", 1], ["beans", 2]]],
  ["Bulk tea (20)", [["tea", 20]]],
];
const money = c => (c < 0 ? "-$" : "$") + (Math.abs(c) / 100).toFixed(2);
const esc = s => String(s).replace(/[&<>"]/g, ch => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[ch]));
async function refresh() {
  try {
    const health = await (await fetch("health")).json();
    const flags = Object.entries(health.flags || {}).map(([k, v]) =>
      v ? `<span class="flag-on">${esc(k)}: on</span>` : `${esc(k)}: off`).join(", ") || "none";
    document.getElementById("status").innerHTML =
      `environment <b>${esc(health.environment)}</b> · revision <b>${esc(health.revision)}</b> · flags ${flags}`;
    const products = await (await fetch("products")).json();
    document.getElementById("products").innerHTML = Object.values(products).map(p =>
      `<div class="card"><div class="name">${esc(p.name)}</div><div class="price">${money(p.price_cents)}</div></div>`
    ).join("");
    let broken = false;
    const rows = await Promise.all(BASKETS.map(async ([name, items]) => {
      const r = await fetch("orders/quote", {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({customer_id: "storefront", items: items.map(([sku, quantity]) => ({sku, quantity}))})});
      const q = await r.json();
      const bad = q.total_cents < q.subtotal_cents / 2;
      broken = broken || bad;
      return `<tr><td>${esc(name)}</td><td>${money(q.subtotal_cents)}</td>
        <td class="total ${bad ? "bad" : "ok"}">${money(q.total_cents)}</td><td>${esc(q.pricing)}</td></tr>`;
    }));
    document.getElementById("quotes").innerHTML = rows.join("");
    document.getElementById("warn").style.display = broken ? "block" : "none";
  } catch (e) {
    document.getElementById("status").textContent = "unreachable: " + e;
  }
}
refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""
