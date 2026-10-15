const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, method = "GET", body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  // Resolve against the origin, not the page URL: a page opened as http://user:pass@host/ can't fetch relative URLs.
  const r = await fetch(new URL(path, location.origin + location.pathname), opts);
  if (r.status === 401) {  // signed out (or the session expired): go to the sign-in page
    location.href = "login" + location.hash;
    throw new Error("Please sign in");
  }
  let data = null;
  try { data = await r.json(); } catch { /* empty body */ }
  if (!r.ok) throw new Error((data && data.detail) || `HTTP ${r.status}`);
  return data;
}

function ago(iso) {
  if (!iso) return "never";
  const s = Math.round((Date.now() - new Date(iso)) / 1000);
  const f = (v, u) => `${v} ${u}${v === 1 ? "" : "s"}`;
  if (s < 0) { const m = Math.round(-s / 60); return m < 60 ? `in ${f(m, "min")}` : `in ${f(Math.round(m / 60), "hour")}`; }
  if (s < 60) return "just now";
  if (s < 3600) return `${f(Math.round(s / 60), "min")} ago`;
  if (s < 86400) return `${f(Math.round(s / 3600), "hour")} ago`;
  return `${f(Math.round(s / 86400), "day")} ago`;
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // Clipboard API needs HTTPS; fall back for plain-HTTP LAN access.
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    return ok;
  }
}

// In-app confirmation and message dialogs (instead of the browser's confirm() / alert()).
const UI = (() => {
  let resolve = null;
  let returnFocus = null;

  function open({ title, message, ok = "OK", cancel = "Cancel", danger = false }) {
    $("dialogTitle").textContent = title;
    $("dialogMsg").innerHTML = String(message || "").split(/\n\s*\n/)
      .map(p => `<p>${esc(p).replace(/\n/g, "<br>")}</p>`).join("");
    $("dialogOk").textContent = ok;
    $("dialogOk").className = danger ? "danger-fill" : "primary";
    $("dialogCancel").hidden = cancel === null;
    $("dialogCancel").textContent = cancel || "";
    returnFocus = document.activeElement;
    $("dialog").hidden = false;
    $("dialogOk").focus();
    return new Promise(r => { resolve = r; });
  }

  function close(result) {
    if (!resolve) return;
    $("dialog").hidden = true;
    const r = resolve;
    resolve = null;
    if (returnFocus && document.contains(returnFocus)) returnFocus.focus();
    r(result);
  }

  document.addEventListener("DOMContentLoaded", () => {
    $("dialogOk").addEventListener("click", () => close(true));
    $("dialogCancel").addEventListener("click", () => close(false));
    $("dialog").addEventListener("click", (e) => { if (e.target === $("dialog")) close(false); });
  });
  document.addEventListener("keydown", (e) => {
    if (!resolve) return;
    if (e.key === "Escape") { e.stopImmediatePropagation(); close(false); }
  }, true);

  return {
    confirm: (opts) => open(opts),
    alert: (title, message) => open({ title, message, cancel: null }),
  };
})();

function route() {
  const settings = location.hash === "#settings";
  $("dashboardView").hidden = settings;
  $("settingsView").hidden = !settings;
  $("navDashboard").classList.toggle("active", !settings);
  $("navSettings").classList.toggle("active", settings);
  if (settings) Settings.open(); else Settings.close();
}
window.addEventListener("hashchange", route);
document.addEventListener("DOMContentLoaded", () => {
  route();
  Dashboard.load();
  $("logoutBtn").addEventListener("click", async () => {
    await api("api/logout", "POST", {}).catch(() => {});
    location.href = "login";
  });
});
