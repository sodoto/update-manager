const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, method = "GET", body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
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

function route() {
  const settings = location.hash === "#settings";
  $("dashboardView").hidden = settings;
  $("settingsView").hidden = !settings;
  $("navDashboard").classList.toggle("active", !settings);
  $("navSettings").classList.toggle("active", settings);
  if (settings) Settings.open(); else Settings.close();
}
window.addEventListener("hashchange", route);
document.addEventListener("DOMContentLoaded", () => { route(); Dashboard.load(); });
