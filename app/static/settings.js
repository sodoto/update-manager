const Settings = (() => {
  const view = $("settingsView");
  let data = null;      // settings being edited
  let saved = null;     // last saved copy (JSON), to detect changes
  let meta = {};
  const tests = {};     // "proxmox.0" -> {ok, message, busy}

  const NEW = {
    proxmox: () => ({ name: "", host: "", port: 22, username: "root", host_key: null, check_host: true, check_lxc: true,
      check_docker_in_lxc: true, exclude: [], dockhand_url: null }),
    docker_hosts: () => ({ name: "", url: "", tls_verify: true, dockhand_url: null }),
    registries: () => ({ registry: "", username: "", password: "" }),
  };

  // ------------------------------------------------------------- helpers
  const getPath = (path) => path.split(".").reduce((o, k) => o?.[k], data);
  function setPath(path, value) {
    const keys = path.split(".");
    const last = keys.pop();
    keys.reduce((o, k) => o[k], data)[last] = value;
  }
  const dirty = () => data && JSON.stringify(data) !== saved;

  function input(label, path, { type = "text", hint = "", placeholder = "", kind = type } = {}) {
    let value = getPath(path);
    if (kind === "intlist") value = (value || []).join(", ");
    const auto = type === "password" ? ' autocomplete="new-password"' : ' autocomplete="off"';
    return `<div class="field"><label>${esc(label)}</label>
      <input type="${type}" data-path="${path}" data-kind="${kind}" value="${esc(value ?? "")}" placeholder="${esc(placeholder)}"${auto}>
      ${hint ? `<div class="hint">${hint}</div>` : ""}</div>`;
  }
  function checkbox(label, path, hint = "") {
    return `<label class="check"><input type="checkbox" data-path="${path}" data-kind="bool" ${getPath(path) ? "checked" : ""}>
      <span>${esc(label)}${hint ? `<br><span class="hint">${hint}</span>` : ""}</span></label>`;
  }
  function testResult(key) {
    const t = tests[key];
    if (!t) return "";
    if (t.busy) return `<div class="test-result"><span class="spin"></span> Testing…</div>`;
    return `<div class="test-result ${t.ok ? "ok" : "bad"}">${esc(t.message)}</div>`;
  }

  // ------------------------------------------------------------- render
  function render() {
    const s = data;
    view.innerHTML = `
      <section class="panel">
        <h2>General</h2>
        <p class="help">How often Update Manager looks for updates.</p>
        <div class="grid">
          ${input("Check every (minutes)", "check_interval_minutes", { type: "number", kind: "int", hint: "Minimum 5. 360 = every 6 hours." })}
          ${input("Parallel checks", "concurrency", { type: "number", kind: "int", hint: "How many containers are checked at the same time." })}
        </div>
        <div class="checks">
          ${checkbox("Check when the app starts", "check_on_start")}
          ${checkbox("Refresh package lists before checking", "refresh_package_lists",
            "Runs <code>apt-get update</code> / <code>apk update</code> to download the latest list of available versions. It never installs anything. If off, results are only as fresh as the last time the container refreshed its lists by itself.")}
        </div>
      </section>

      <section class="panel">
        <h2>Update Manager updates</h2>
        <p class="help">New versions of this app are published on GitHub.</p>
        <div class="checks">
          ${checkbox("Check GitHub for new versions", "check_app_updates", "Every 6 hours. Shows a banner on the dashboard when one is available.")}
          ${checkbox("Allow updating Update Manager from the dashboard", "allow_self_update",
            "Adds an <b>Update now</b> button (LXC installs only). It only updates this app, never your containers or Proxmox.")}
        </div>
        <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:12px">
          <button class="small" data-action="check-app">Check for updates now</button>
          <span class="help" style="margin:0" id="appVersionInfo"></span>
        </div>
      </section>

      <section class="panel">
        <h2>Run updates from the app</h2>
        <p class="help">Off: Update Manager is strictly <b>read-only</b>. On: an <b>Update</b> button installs package updates in LXCs
          and updates Docker Compose containers. It uses a <b>second, separate key</b> that can only do that; checks keep using the read-only key.</p>
        ${s.ui_username && s.ui_password ? "" : `<div class="warn">Set a dashboard login first (Dashboard login, below). It is required to turn this on.</div>`}
        <div class="checks">${checkbox("Allow Update Manager to install updates", "allow_updates")}</div>
        ${s.allow_updates ? `
        <details class="setup" open>
          <summary>Update access: run once on each Proxmox node</summary>
          <ol>
            <li>In Proxmox, open <b>your node → Shell</b>, paste the block below and press Enter.</li>
            <li>Save these settings, then click <b>Test update access</b> on the node below.</li>
          </ol>
          <textarea class="code" readonly>${esc(meta.update_setup_script)}</textarea>
          <div style="margin-top:8px"><button class="small" data-action="copy" data-copy-key="update_setup_script">Copy</button></div>
          <p class="help" style="margin-top:10px">Installs <code>/usr/local/bin/update-manager-apply</code>. That key can <b>only</b>: install package
            updates in an LXC, restart an LXC, and update one Docker Compose service (folder and service read from the container's own labels).
            Nothing else: no shell, no other commands.</p>
          <p class="help">To go back to read-only, uncheck the box above and run this on each node:</p>
          <textarea class="code" readonly style="height:60px">${esc(meta.update_revoke_script)}</textarea>
          <div style="margin-top:8px"><button class="small" data-action="copy" data-copy-key="update_revoke_script">Copy</button></div>
        </details>` : ""}
      </section>

      <section class="panel">
        <h2>Proxmox nodes</h2>
        <p class="help">Every running LXC on these nodes is checked, plus the node itself. Docker inside an LXC is detected automatically.
          Access is <b>read-only</b>: the app's SSH key can only run a small gateway script that allows reading, never changing.</p>
        <details class="setup" ${s.proxmox.length ? "" : "open"}>
          <summary>Read-only setup: run once on each Proxmox node</summary>
          <ol>
            <li>In Proxmox, open <b>your node → Shell</b>.</li>
            <li>Paste the block below and press Enter.</li>
            <li>Add the node here and click <b>Test connection</b>.</li>
          </ol>
          <textarea class="code" readonly id="setupScript">${esc(meta.setup_script)}</textarea>
          <div style="margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
            <button class="small" data-action="copy-setup">Copy</button>
            <span class="help" style="margin:0">Installs <code>/usr/local/bin/update-manager-ro</code> and authorizes this app's key <b>only</b> for that script
              (<code>command=…,restrict</code> in <code>authorized_keys</code>): no shell, no forwarding, nothing else.</span>
          </div>
        </details>
        ${s.proxmox.map((n, i) => `
          <div class="sub-card">
            <div class="head"><b>${esc(n.name || "New node")}</b>
              <button class="small" data-action="test-proxmox" data-index="${i}">Test connection</button>
              ${s.allow_updates ? `<button class="small" data-action="test-apply" data-index="${i}">Test update access</button>` : ""}
              <button class="small danger" data-action="remove" data-list="proxmox" data-index="${i}">Remove</button></div>
            <div class="grid">
              ${input("Name", `proxmox.${i}.name`, { placeholder: "pve" })}
              ${input("Host / IP", `proxmox.${i}.host`, { placeholder: "192.168.1.10" })}
              ${input("SSH port", `proxmox.${i}.port`, { type: "number", kind: "int" })}
              ${input("SSH user", `proxmox.${i}.username`)}
              ${input("Skip these LXC IDs", `proxmox.${i}.exclude`, { kind: "intlist", placeholder: "e.g. 105, 120" })}
              ${input("Fallback Dockhand URL (optional)", `proxmox.${i}.dockhand_url`, { placeholder: "http://192.168.1.20:3000",
                hint: "Dockhand running inside an LXC is found automatically. This link is only used when it isn't." })}
            </div>
            <div class="checks">
              ${checkbox("Check the Proxmox host's own packages", `proxmox.${i}.check_host`)}
              ${checkbox("Check packages inside LXCs", `proxmox.${i}.check_lxc`)}
              ${checkbox("Find Docker containers inside LXCs", `proxmox.${i}.check_docker_in_lxc`)}
            </div>
            <div class="field" style="margin-top:12px"><label>Trusted SSH host key</label>
              <div style="display:flex;gap:8px"><input readonly value="${esc(n.host_key || "")}" placeholder="Not set: filled in automatically by Test connection">
              ${n.host_key ? `<button class="small" data-action="forget-key" data-index="${i}">Forget</button>` : ""}</div>
              <div class="hint">Protects against connecting to the wrong machine.</div></div>
            ${testResult(`proxmox.${i}`)}
            ${testResult(`apply.${i}`)}
          </div>`).join("")}
        <button class="add" data-action="add" data-list="proxmox">+ Add Proxmox node</button>
      </section>

      <section class="panel">
        <h2>Docker hosts</h2>
        <p class="help">Only needed for Docker that does <b>not</b> run inside an LXC on a node above (a VM, a NAS, the machine running this app…).
          For read-only access, point it at a <a href="https://github.com/Tecnativa/docker-socket-proxy" target="_blank" rel="noopener">docker-socket-proxy</a>
          with <code>CONTAINERS=1 IMAGES=1 POST=0</code>. The app only ever sends GET requests.</p>
        ${s.docker_hosts.map((h, i) => `
          <div class="sub-card">
            <div class="head"><b>${esc(h.name || "New Docker host")}</b>
              <button class="small" data-action="test-docker" data-index="${i}">Test connection</button>
              <button class="small danger" data-action="remove" data-list="docker_hosts" data-index="${i}">Remove</button></div>
            <div class="grid">
              ${input("Name", `docker_hosts.${i}.name`, { placeholder: "nas" })}
              ${input("Docker API URL", `docker_hosts.${i}.url`, { placeholder: "tcp://192.168.1.30:2375", hint: "tcp://host:port, https://host:port or unix:///var/run/docker.sock" })}
              ${input("Fallback Dockhand URL (optional)", `docker_hosts.${i}.dockhand_url`, {
                hint: "Found automatically when Dockhand runs on this host." })}
            </div>
            <div class="checks">${checkbox("Verify TLS certificate (https only)", `docker_hosts.${i}.tls_verify`)}</div>
            ${testResult(`docker_hosts.${i}`)}
          </div>`).join("")}
        <button class="add" data-action="add" data-list="docker_hosts">+ Add Docker host</button>
      </section>

      <section class="panel">
        <h2>Registry logins</h2>
        <p class="help">Optional. Only for private images, or to avoid Docker Hub rate limits. A read-only access token is enough.</p>
        ${s.registries.map((r, i) => `
          <div class="sub-card">
            <div class="head"><b>${esc(r.registry || "New registry")}</b>
              <button class="small danger" data-action="remove" data-list="registries" data-index="${i}">Remove</button></div>
            <div class="grid">
              ${input("Registry", `registries.${i}.registry`, { placeholder: "docker.io, ghcr.io…" })}
              ${input("Username", `registries.${i}.username`)}
              ${input("Password / token", `registries.${i}.password`, { type: "password" })}
            </div>
          </div>`).join("")}
        <button class="add" data-action="add" data-list="registries">+ Add registry login</button>
      </section>

      <section class="panel">
        <h2>Community scripts</h2>
        <p class="help">LXCs created with the <a href="https://community-scripts.github.io/ProxmoxVE/" target="_blank" rel="noopener">Proxmox VE
          community scripts</a> are detected automatically, and their app version is compared with the latest release on GitHub.
          GitHub allows 60 lookups per hour without a token (results are cached for 6 hours). If you see a rate-limit message,
          add a token: GitHub → Settings → Developer settings → Fine-grained token, with <b>no permissions</b> (public data only).</p>
        <div class="grid">${input("GitHub token (optional)", "github_token", { type: "password" })}</div>
      </section>

      <section class="panel">
        <h2>Notifications</h2>
        <p class="help">Get a message when <b>new</b> updates are found. Leave empty to disable.</p>
        <div class="grid">
          ${input("ntfy topic URL", "notify.ntfy_url", { placeholder: "https://ntfy.sh/my-homelab" })}
          ${input("ntfy access token", "notify.ntfy_token", { type: "password" })}
          ${input("Discord webhook URL", "notify.discord_webhook", { type: "password" })}
          ${input("Generic webhook URL (JSON POST)", "notify.webhook_url")}
        </div>
        <button class="add" data-action="test-notify">Send test notification</button>
        ${testResult("notify")}
      </section>

      <section class="panel">
        <h2>Dashboard login</h2>
        ${s.ui_username ? "" : `<div class="warn">No login set: anyone on your network can open this dashboard and its settings.</div>`}
        <div class="grid">
          ${input("Username", "ui_username")}
          ${input("Password", "ui_password", { type: "password",
            hint: "Stored encrypted (hashed). To change it, type a new one; leave the dots to keep it." })}
        </div>
        <p class="help" style="margin-top:10px">Settings are stored in <code>${esc(meta.config_path)}</code>.</p>
      </section>`;
    updateSavebar();
    showAppVersion();
  }

  async function showAppVersion(fresh) {
    try {
      const a = fresh || (await api("api/state")).app;
      const el = $("appVersionInfo");
      if (!el) return;
      el.innerHTML = a.error ? `You have v${esc(a.version)}. ${esc(a.error)}`
        : a.update_available ? `You have v${esc(a.version)}. <b>v${esc(a.latest)} is available</b>, see the dashboard banner.`
        : a.latest ? `You have v${esc(a.version)}, the latest version. Checked ${ago(a.checked_at)}.`
        : `You have v${esc(a.version)}.`;
      if (fresh) AppUpdate.render(fresh);
    } catch { /* not critical */ }
  }

  function updateSavebar(message, cls = "") {
    const show = dirty() || message;
    $("savebar").hidden = !show || view.hidden;
    $("saveMsg").textContent = message || "You have unsaved changes";
    $("saveMsg").className = `msg ${cls}`;
    $("saveBtn").disabled = !dirty();
    $("discardBtn").hidden = !dirty();
  }

  // ------------------------------------------------------------- actions
  async function load() {
    view.innerHTML = `<div class="empty"><span class="spin"></span></div>`;
    try {
      meta = await api("api/settings");
      data = meta.settings;
      saved = JSON.stringify(data);
      render();
    } catch (e) {
      view.innerHTML = `<div class="empty">Could not load settings: ${esc(e.message)}</div>`;
    }
  }

  async function save() {
    $("saveBtn").disabled = true;
    try {
      const res = await api("api/settings", "PUT", data);
      data = res.settings;
      saved = JSON.stringify(data);
      render();
      updateSavebar(res.check_started ? "Saved. A check has started." : "Saved.", "ok");
      setTimeout(() => updateSavebar(), 4000);
      Dashboard.load();
    } catch (e) {
      updateSavebar(`Not saved: ${e.message}`, "bad");
      $("saveBtn").disabled = false;
    }
  }

  async function runTest(key, url, body, after) {
    tests[key] = { busy: true };
    render();
    try {
      const res = await api(url, "POST", body);
      tests[key] = res;
      if (after) after(res);
    } catch (e) {
      tests[key] = { ok: false, message: e.message };
    }
    render();
  }

  view.addEventListener("input", (e) => {
    const el = e.target;
    if (!el.dataset.path) return;
    const kind = el.dataset.kind;
    let value = el.value;
    if (kind === "bool") value = el.checked;
    else if (kind === "int") value = value === "" ? null : Number(value);
    else if (kind === "intlist") value = value.split(/[\s,;]+/).filter(Boolean).map(Number).filter(Number.isInteger);
    else if (value === "") value = null;
    setPath(el.dataset.path, value);
    if (el.dataset.path === "allow_updates") render();  // show / hide the update setup block
    updateSavebar();
  });

  view.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-action]");
    if (!btn) return;
    const i = Number(btn.dataset.index);
    switch (btn.dataset.action) {
      case "add":
        data[btn.dataset.list].push(NEW[btn.dataset.list]());
        render();
        break;
      case "remove":
        data[btn.dataset.list].splice(i, 1);
        Object.keys(tests).filter(k => k.startsWith(btn.dataset.list)).forEach(k => delete tests[k]);
        render();
        break;
      case "forget-key":
        data.proxmox[i].host_key = null;
        render();
        break;
      case "copy-setup":
        btn.textContent = (await copyText(meta.setup_script)) ? "Copied!" : "Copy failed: select and copy manually";
        setTimeout(() => { btn.textContent = "Copy"; }, 2500);
        break;
      case "test-proxmox":
        await runTest(`proxmox.${i}`, "api/test/proxmox", data.proxmox[i], (res) => {
          if (res.host_key && !data.proxmox[i].host_key) {
            data.proxmox[i].host_key = res.host_key;
            res.message += " · Host key trusted; save to keep it.";
          }
        });
        break;
      case "test-apply":
        await runTest(`apply.${i}`, "api/test/apply", data.proxmox[i]);
        break;
      case "copy":
        btn.textContent = (await copyText(meta[btn.dataset.copyKey])) ? "Copied!" : "Copy failed: select and copy manually";
        setTimeout(() => { btn.textContent = "Copy"; }, 2500);
        break;
      case "test-docker":
        await runTest(`docker_hosts.${i}`, "api/test/docker", data.docker_hosts[i]);
        break;
      case "check-app":
        btn.disabled = true;
        $("appVersionInfo").innerHTML = `<span class="spin"></span>`;
        try { await showAppVersion(await api("api/app/check", "POST", {})); }
        catch (e) { $("appVersionInfo").textContent = e.message; }
        btn.disabled = false;
        break;
      case "test-notify":
        await runTest("notify", "api/test/notify", data.notify);
        break;
    }
  });

  $("saveBtn").addEventListener("click", save);
  $("discardBtn").addEventListener("click", () => { data = JSON.parse(saved); render(); });
  window.addEventListener("beforeunload", (e) => { if (dirty()) e.preventDefault(); });

  return {
    open() { if (data) render(); else load(); },
    close() { $("savebar").hidden = true; },
  };
})();
