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

  // -- live: reload only when the server says something changed -----------------
  const live = document.querySelector("[data-live]");
  if (live) {
    const url = live.dataset.live;
    let version = live.dataset.version;
    const SCROLL = "rewind-scroll:" + location.pathname;
    try {
      const y = sessionStorage.getItem(SCROLL);
      if (y) { window.scrollTo(0, +y); sessionStorage.removeItem(SCROLL); }
    } catch { /* ignore */ }
    const poll = async () => {
      if (document.hidden) return;
      try {
        const res = await fetch(url, { cache: "no-store" });
        if (!res.ok) return;
        const body = await res.json();
        if (body.version && body.version !== version) {
          version = body.version;
          try { sessionStorage.setItem(SCROLL, String(window.scrollY)); } catch { /* ignore */ }
          location.reload();
        }
      } catch { /* offline: try again next time */ }
    };
    setInterval(poll, +(live.dataset.interval || 4000));
  }
})();
