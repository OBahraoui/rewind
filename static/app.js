// Rewind dashboard: theme toggle, live updates without page flashes, ticking clocks.
(() => {
  "use strict";
  const root = document.documentElement;
  const KEY = "rewind-theme";
  const store = {
    get() { try { return localStorage.getItem(KEY); } catch { return null; } },
    set(v) { try { v ? localStorage.setItem(KEY, v) : localStorage.removeItem(KEY); } catch { /* private mode */ } },
  };

  // -- theme: system -> light -> dark -> system ---------------------------------
  const applyTheme = (t) => { if (t) root.dataset.theme = t; else delete root.dataset.theme; };
  applyTheme(store.get());
  const toggle = document.querySelector("[data-theme-toggle]");
  const label = () => {
    const t = root.dataset.theme || "system";
    if (toggle) {
      toggle.setAttribute("aria-label", `Theme: ${t}. Change theme`);
      toggle.title = `Theme: ${t}`;
      toggle.dataset.mode = t;
    }
  };
  label();
  toggle?.addEventListener("click", () => {
    const next = { system: "light", light: "dark", dark: null }[root.dataset.theme || "system"];
    applyTheme(next);
    store.set(next);
    label();
  });

  // -- relative times and elapsed clocks -----------------------------------------
  const fmtAgo = (iso) => {
    const s = (Date.now() - Date.parse(iso)) / 1000;
    if (!isFinite(s)) return "—";
    if (s < 45) return "just now";
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    return new Date(iso).toLocaleDateString(undefined, { day: "2-digit", month: "short" });
  };
  const fmtElapsed = (iso) => {
    let s = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 1000));
    const h = Math.floor(s / 3600); s -= h * 3600;
    const m = Math.floor(s / 60); s -= m * 60;
    return h ? `${h}h ${String(m).padStart(2, "0")}m` : `${m}m ${String(s).padStart(2, "0")}s`;
  };
  const tick = () => {
    document.querySelectorAll("[data-ago]").forEach((el) => { el.textContent = fmtAgo(el.dataset.ago); });
    document.querySelectorAll("[data-since]").forEach((el) => { el.textContent = fmtElapsed(el.dataset.since); });
  };
  tick();
  setInterval(tick, 1000);

  // -- copy buttons --------------------------------------------------------------
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-copy]");
    if (!btn) return;
    try {
      await navigator.clipboard.writeText(btn.dataset.copy);
    } catch {
      const sel = window.getSelection(), range = document.createRange();
      range.selectNodeContents(btn.previousElementSibling);
      sel.removeAllRanges(); sel.addRange(range);
      return; // selected for the user to copy by hand
    }
    btn.classList.add("done");
    announce("Copied to the clipboard");
    setTimeout(() => btn.classList.remove("done"), 1600);
  });

  // -- live: when the server says something changed, swap the page in place --------
  // No reload: scroll, focus, open sections and the screen reader's place survive; the change is announced once.
  const announcer = document.getElementById("live-announce");
  function announce(text) {
    if (!announcer || !text) return;
    announcer.textContent = "";
    setTimeout(() => { announcer.textContent = text; }, 50);
  }
  const focusKey = (el) => {
    if (!el || el === document.body) return null;
    if (el.id) return `#${CSS.escape(el.id)}`;
    if (el.dataset.copy) return `[data-copy="${CSS.escape(el.dataset.copy)}"]`;
    const href = el.getAttribute("href");
    return href ? `a[href="${CSS.escape(href)}"]` : null;
  };
  async function swap() {
    const res = await fetch(location.href, { cache: "no-store", headers: { accept: "text/html" } });
    if (!res.ok) return false;
    const doc = new DOMParser().parseFromString(await res.text(), "text/html");
    const fresh = doc.querySelector("main"), main = document.querySelector("main");
    if (!fresh || !main) return false;
    const key = focusKey(document.activeElement);
    const open = new Set([...main.querySelectorAll("details[open] > summary")].map((s) => s.textContent.trim()));
    main.replaceChildren(...[...fresh.childNodes].map((n) => document.importNode(n, true)));
    main.querySelectorAll("details > summary").forEach((s) => { if (open.has(s.textContent.trim())) s.parentElement.open = true; });
    document.title = doc.title;
    if (key) main.querySelector(key)?.focus({ preventScroll: true });
    tick();
    const next = main.querySelector("[data-live]");
    const desc = doc.querySelector('meta[name="description"]')?.content || "";
    announce(next?.dataset.announce || (next ? "Runs updated" : desc));
    return true;
  }
  let version = document.querySelector("[data-live]")?.dataset.version;
  async function poll() {
    const live = document.querySelector("[data-live]");
    if (!live) return; // the run finished: nothing more will change
    setTimeout(poll, +(live.dataset.interval || 4000));
    if (document.hidden) return;
    try {
      const res = await fetch(live.dataset.live, { cache: "no-store" });
      if (!res.ok) return;
      const body = await res.json();
      if (body.version && body.version !== version) {
        version = body.version;
        if (!(await swap())) location.reload();
      }
    } catch { /* offline: try again next time */ }
  }
  const first = document.querySelector("[data-live]");
  if (first) setTimeout(poll, +(first.dataset.interval || 4000));
})();
