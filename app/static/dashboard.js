const Dashboard = (() => {
  const KIND = { docker: "Docker", lxc: "LXC", host: "Host" };
  const STATUS = { update_available: "Update available", up_to_date: "Up to date", error: "Error", skipped: "Skipped" };
  const ORDER = { update_available: 0, error: 1, up_to_date: 2, skipped: 3 };
  let state = null;
  let filter = "all";
  let timer;
  const open = new Set();

  async function load() {
    clearTimeout(timer);
    try {
      state = await api("api/state");
      $("version").textContent = `v ${state.version}`;
      render();
    } catch (e) {
      $("meta").textContent = `Cannot reach server (${e.message})`;
    }
    timer = setTimeout(load, state && state.running ? 3000 : 30000);
  }

  function render() {
    const items = state.items;
    const count = (fn) => items.filter(fn).length;
    const btn = $("checkBtn");
    btn.disabled = state.running;
    btn.innerHTML = state.running ? '<span class="spin"></span> Checking…' : "Check now";
    $("meta").textContent = state.running ? "Check in progress…"
      : `Last check ${ago(state.last_check_finished)}` + (state.next_check ? ` · next ${ago(state.next_check)}` : "");

    const cards = [
      ["all", "", items.length, "Monitored"],
      ["updates", "upd", count(i => i.status === "update_available"), "Need updates"],
      ["ok", "ok", count(i => i.status === "up_to_date"), "Up to date"],
      ["errors", "err", count(i => i.status === "error"), "Errors"],
    ];
    $("cards").innerHTML = cards.map(([key, cls, n, label]) =>
      `<div class="card ${cls} ${filter === key ? "active" : ""}" data-filter="${key}"><div class="n">${n}</div><div class="l">${label}</div></div>`).join("");

    const banner = (list, cls) => list && list.length
      ? `<div class="banner ${cls}">${list.map(e => `<div><b>${esc(e.source)}:</b> ${esc(e.message)}</div>`).join("")}</div>` : "";
    $("banner").innerHTML = banner(state.errors, "") + banner(state.warnings, "warn-banner");

    if (!state.configured) {
      $("list").innerHTML = `<div class="empty"><b>Nothing configured yet.</b><br>Go to <a href="#settings">Settings</a> to add your Proxmox nodes and Docker hosts.</div>`;
      return;
    }
    if (!items.length) {
      $("list").innerHTML = `<div class="empty">${state.running ? "First check running…" : "No results yet. Click <b>Check now</b>."}</div>`;
      return;
    }

    const q = $("search").value.trim().toLowerCase();
    const kind = $("kind").value;
    const shown = items.filter(i =>
      (filter === "all" || (filter === "updates" && i.status === "update_available")
        || (filter === "ok" && i.status === "up_to_date") || (filter === "errors" && i.status === "error"))
      && (!kind || i.kind === kind)
      && (!q || [i.name, i.detail, i.source, i.note].join(" ").toLowerCase().includes(q)));
    if (!shown.length) { $("list").innerHTML = `<div class="empty">Nothing matches this filter.</div>`; return; }

    const groups = {};
    for (const i of shown) (groups[i.source] ??= []).push(i);
    $("list").innerHTML = Object.keys(groups).sort().map(src => {
      const list = groups[src].sort((a, b) => (ORDER[a.status] - ORDER[b.status])
        || (a.kind === b.kind ? 0 : a.kind === "host" ? -1 : b.kind === "host" ? 1 : a.kind.localeCompare(b.kind))
        || a.name.localeCompare(b.name));
      const upd = list.filter(i => i.status === "update_available").length;
      return `<section class="group"><h2>${esc(src)} <span class="count">· ${list.length} item${list.length === 1 ? "" : "s"}${upd ? ` · ${upd} to update` : ""}</span></h2>${list.map(row).join("")}</section>`;
    }).join("");
  }

  function row(i) {
    const badges = [`<span class="badge kind">${KIND[i.kind]}</span>`];
    if (i.status === "update_available") {
      badges.push(`<span class="badge update_available">${i.kind === "docker" ? "New image" : `${i.update_count} package${i.update_count === 1 ? "" : "s"}`}</span>`);
      if (i.security_count) badges.push(`<span class="badge sec">${i.security_count} security</span>`);
    } else {
      badges.push(`<span class="badge ${i.status}">${STATUS[i.status]}</span>`);
    }
    if (i.reboot_required) badges.push(`<span class="badge update_available">Reboot required</span>`);
    if (i.stale) badges.push(`<span class="badge">Stale</span>`);

    const buttons = [];
    if (i.packages.length || i.kind === "docker" || i.note)
      buttons.push(`<button data-toggle="${esc(i.id)}">${open.has(i.id) ? "Hide" : "Details"}</button>`);
    if (i.link) buttons.push(`<a class="btn" href="${esc(i.link)}" target="_blank" rel="noopener">Dockhand ↗</a>`);

    const detail = i.kind === "docker" ? `<code>${esc(i.detail)}</code>` : esc(i.detail);
    return `<div class="row">
      <span class="dot ${i.status}" title="${STATUS[i.status]}"></span>
      <div><div class="name" title="${esc(i.name)}">${esc(i.name)}</div>${i.kind === "docker" && i.state ? `<div class="sub">${esc(i.state)}</div>` : ""}</div>
      <div class="detail" title="${esc(i.note || i.detail)}">${detail}${i.note && i.status !== "update_available" ? ` — ${esc(i.note)}` : ""}</div>
      <div class="actions">${badges.join("")}${buttons.join("")}</div>
      ${open.has(i.id) ? `<div class="expand">${expand(i)}</div>` : ""}
    </div>`;
  }

  function expand(i) {
    if (i.kind === "docker") {
      return `<table>
        <tr><th>Image</th><td><code>${esc(i.detail)}</code></td></tr>
        <tr><th>Running digest</th><td><code>${esc(i.local_digest || "—")}</code></td></tr>
        <tr><th>Registry digest</th><td><code>${esc(i.remote_digest || "—")}</code></td></tr>
        ${i.note ? `<tr><th>Note</th><td>${esc(i.note)}</td></tr>` : ""}
        <tr><th>Checked</th><td>${ago(i.checked_at)}</td></tr></table>`
        + (i.status === "update_available" ? `<p>Pull the new image and recreate the container (e.g. from Dockhand) to apply.</p>` : "");
    }
    let html = i.note ? `<p>${esc(i.note)}</p>` : "";
    if (i.packages.length) {
      html += `<div class="pkg-scroll"><table><tr><th>Package</th><th>Installed</th><th>Available</th></tr>${
        i.packages.map(p => `<tr><td>${esc(p.name)}${p.security ? ' <span class="badge sec">security</span>' : ""}</td><td>${esc(p.current || "—")}</td><td>${esc(p.new)}</td></tr>`).join("")
      }</table></div>`;
    }
    return html;
  }

  async function checkNow() {
    await api("api/check", "POST", {});
    if (state) { state.running = true; render(); }
    setTimeout(load, 800);
  }

  document.addEventListener("click", (e) => {
    const card = e.target.closest("[data-filter]");
    if (card) { filter = card.dataset.filter; render(); return; }
    const t = e.target.closest("[data-toggle]");
    if (t) { const id = t.dataset.toggle; open.has(id) ? open.delete(id) : open.add(id); render(); }
  });
  $("search").addEventListener("input", () => state && render());
  $("kind").addEventListener("change", () => state && render());
  $("checkBtn").addEventListener("click", checkNow);

  return { load };
})();
