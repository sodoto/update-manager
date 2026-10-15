// Banner and self-update flow for new versions of Update Manager itself.
const AppUpdate = (() => {
  let info = null;
  let updating = null;  // { from, started } while a self-update is in progress
  let checking = false;

  async function checkNow() {
    checking = true;
    const el = $("version");
    el.innerHTML = `<span class="spin"></span> Checking…`;
    let message;
    try {
      const a = await api("api/app/check", "POST", {});
      message = a.error ? `⚠ ${a.error}` : a.update_available ? null : `✓ v${a.version} is the latest`;
      checking = false;
      render(a);
    } catch (e) {
      message = `⚠ ${e.message}`;
    }
    checking = false;
    if (message) {
      el.textContent = message;
      setTimeout(() => render(info), 4000);
    }
  }

  const UPDATE_CMD = {
    lxc: "update-manager-update",
    docker: "cd update-manager && git pull && docker compose up -d --build",
  };

  function notesHtml(a) {
    if (!a.notes.length) return `<p class="hint">No changelog available.</p>`;
    return a.notes.map(n => `<h4>v${esc(n.version)}${n.date ? ` <span class="hint">(${esc(n.date)})</span>` : ""}</h4>
      <ul>${n.items.map(it => `<li>${esc(it).replace(/`([^`]+)`/g, "<code>$1</code>")}</li>`).join("")}</ul>`).join("");
  }

  function render(a) {
    info = a;
    const el = $("appBanner");
    if (updating) {
      el.innerHTML = `<div class="app-banner"><span class="spin"></span>
        <div class="text"><b>Updating Update Manager…</b> This takes about a minute; the page reloads by itself.</div></div>`;
      return;
    }
    if (!checking) {
      $("version").innerHTML = `<button class="link version-btn" data-app="check" title="Check for a new version">v${esc(a.version)}</button>`
        + (a.update_available ? ` <button class="link" data-app="notes" title="A new version is available">→ v${esc(a.latest)}</button>` : "");
    }
    if (!a.update_available) { el.innerHTML = ""; return; }
    el.innerHTML = `<div class="app-banner">
      <div class="text"><b>Update Manager v${esc(a.latest)} is available</b> <span class="hint">· you have v${esc(a.version)}</span></div>
      <button data-app="notes">What's new</button>
      ${a.can_self_update ? `<button class="primary" data-app="update">Update now</button>`
                          : `<button data-app="howto">How to update</button>`}
    </div>`;
  }

  function showNotes() {
    HowTo.show(`<h3 id="modalTitle">What's new in Update Manager</h3>
      <p class="lead">You have v${esc(info.version)}, latest is v${esc(info.latest)}.</p>${notesHtml(info)}
      ${info.can_self_update ? `<p style="margin-top:16px"><button class="primary" data-app="update">Update now</button></p>` : ""}`);
  }

  function showHowto() {
    const cmd = UPDATE_CMD[info.install];
    const where = info.install === "lxc" ? "In the Update Manager LXC's console" : "On the machine running Update Manager";
    HowTo.show(`<h3 id="modalTitle">Update Update Manager to v${esc(info.latest)}</h3>
      ${cmd ? `<p>${where}:</p><div class="cmd"><pre>${esc(cmd)}</pre><button data-copy>Copy</button></div>`
            : `<p>Download the latest version from <a href="https://github.com/${esc(info.repo)}" target="_blank" rel="noopener">GitHub</a> and reinstall.</p>`}
      ${info.install === "lxc" && !info.can_self_update ? `<p class="hint">"Update now" is turned off in Settings → General.</p>` : ""}
      <p class="hint">Your settings are kept.</p>`);
  }

  async function start() {
    if (!confirm(`Update Update Manager from v${info.version} to v${info.latest}?\n\nThe app restarts; this takes about a minute.`)) return;
    try {
      await api("api/app/update", "POST", {});
    } catch (e) {
      alert(`Could not start the update: ${e.message}`);
      return;
    }
    HowTo.close();
    updating = { from: info.version, started: Date.now() };
    render(info);
    setTimeout(wait, 3000);
  }

  async function wait() {
    const elapsed = Date.now() - updating.started;
    try {
      const s = await api("api/state");
      if (s.version !== updating.from) { location.reload(); return; }
      if (elapsed > 60000) {
        const { log } = await api("api/app/update-log");
        const exit = /Exit code: (\d+)/.exec(log || "");
        if (exit && exit[1] !== "0") return failed(log);
      }
    } catch { /* the app is restarting */ }
    if (elapsed > 5 * 60000) {
      const res = await api("api/app/update-log").catch(() => ({ log: "" }));
      return failed(res.log);
    }
    setTimeout(wait, 3000);
  }

  function failed(log) {
    updating = null;
    render(info);
    HowTo.show(`<h3 id="modalTitle">The update did not finish</h3>
      <p>Update Manager is still on v${esc(info.version)}. You can run the update by hand in the LXC console:</p>
      <div class="cmd"><pre>update-manager-update</pre><button data-copy>Copy</button></div>
      ${log ? `<h4>Update log</h4><div class="cmd"><pre>${esc(log)}</pre></div>` : ""}`);
  }

  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-app]");
    if (!b || !info || checking) return;
    if (b.dataset.app === "check") checkNow();
    else if (b.dataset.app === "notes") showNotes();
    else if (b.dataset.app === "howto") showHowto();
    else if (b.dataset.app === "update") start();
  });

  return { render, isUpdating: () => !!updating };
})();
