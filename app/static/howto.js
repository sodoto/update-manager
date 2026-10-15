// "How to update" instructions shown in a modal. Update Manager is read-only:
// it only shows the commands, the user runs them.
const HowTo = (() => {
  const UPGRADE = {
    apt: "apt update && apt full-upgrade -y",
    apk: "apk update && apk upgrade",
    dnf: "dnf upgrade -y",
    pacman: "pacman -Syu --noconfirm",
  };
  const KERNEL = /^(proxmox-kernel|pve-kernel|linux-image)/;

  const where = (label) => `<span class="where">${esc(label)}</span>`;
  const cmd = (text) => `<div class="cmd"><pre>${esc(text)}</pre><button data-copy>Copy</button></div>`;
  const shq = (s) => `'${String(s).replace(/'/g, `'\\''`)}'`;  // shell single-quote
  const arg = (s) => /^[\w@%+=:,./-]+$/.test(s) ? s : shq(s);  // quote only when needed
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

  function lxc(i) {
    const id = i.vmid;
    const up = UPGRADE[i.manager];
    const kernel = i.packages.some(p => KERNEL.test(p.name));
    let html = `<h3 id="modalTitle">Update ${esc(i.name)} (CT ${id})</h3>
      <p class="lead">${plural(i.update_count, "package")} to update${i.security_count ? `, including ${i.security_count} security` : ""}.</p>

      <h4>1. Optional: take a snapshot first</h4>
      <p>${where("Proxmox Shell")} Lets you roll back if something breaks.</p>
      ${cmd(`pct snapshot ${id} before-update`)}
      <p class="hint">Needs storage with snapshot support (ZFS, LVM-thin, Ceph). Otherwise use CT ${id} → Backup → Backup now.
        To roll back: <code>pct rollback ${id} before-update</code>.</p>

      <h4>2. Install the updates</h4>`;
    if (up) {
      html += `<p>${where("Proxmox Shell")} Datacenter → ${esc(i.node || "your node")} → Shell, then:</p>
        ${cmd(`pct exec ${id} -- sh -c ${shq(up)}`)}
        <p class="hint">Or open CT ${id} → Console and run <code>${esc(up)}</code> directly.</p>`;
    } else {
      html += `<p>Open CT ${id} → Console and use the container's package manager to upgrade.</p>`;
    }
    if (i.reboot_required || kernel) {
      html += `<h4>3. Restart the container</h4>
        <p>${where("Proxmox Shell")} ${i.reboot_required ? "The container reports that a restart is required." : "Some updated services only reload after a restart."}</p>
        ${cmd(`pct reboot ${id}`)}`;
    }
    html += `<div class="note"><b>Created with a Proxmox VE helper script?</b> Those LXCs also have an <code>update</code> command
      that updates the app itself (not just the OS packages): run <code>update</code> in CT ${id}'s console.</div>`;
    return html;
  }

  function host(i) {
    const kernel = i.packages.some(p => KERNEL.test(p.name));
    return `<h3 id="modalTitle">Update Proxmox host ${esc(i.name)}</h3>
      <p class="lead">${plural(i.update_count, "package")} to update${i.security_count ? `, including ${i.security_count} security` : ""}${kernel ? ", including a new kernel" : ""}.</p>

      <h4>Option A: from the web UI</h4>
      <p>Datacenter → ${esc(i.name)} → <b>Updates</b> → <b>Refresh</b>, then <b>Upgrade</b>. A window opens and runs the upgrade; answer <code>Y</code> when asked.</p>

      <h4>Option B: from the shell</h4>
      <p>${where("Proxmox Shell")}</p>
      ${cmd("apt update && apt full-upgrade -y")}
      <p class="hint">On Proxmox, always use <code>full-upgrade</code> (or <code>dist-upgrade</code>), never plain <code>apt upgrade</code>.</p>
      ${kernel || i.reboot_required ? `<h4>Then reboot the node</h4>
        <p>A new kernel is only used after a reboot. Shut down or migrate VMs and containers first, then:</p>
        ${cmd("reboot")}` : ""}`;
  }

  function docker(i) {
    const inLxc = i.vmid != null;
    const run = (c) => inLxc ? `pct exec ${i.vmid} -- sh -c ${shq(c)}` : c;
    const place = inLxc ? where("Proxmox Shell") : where(`On ${i.source}`);
    let html = `<h3 id="modalTitle">Update container ${esc(i.name)}</h3>
      <p class="lead">A newer image was published for <code>${esc(i.detail)}</code>. Updating means pulling the new image
        and recreating the container. Data in volumes and bind mounts is kept.</p>

      <h4>Option A: Dockhand (easiest)</h4>
      <p>${i.link ? `<a class="btn" href="${esc(i.link)}" target="_blank" rel="noopener">Open Dockhand ↗</a>` : "Open the Dockhand that manages this container."}</p>
      <p class="hint">Find the container (or its stack) and use its update / redeploy action, which pulls the new image and recreates it.</p>

      <h4>Option B: command line</h4>`;
    if (i.compose_dir) {
      const svc = i.compose_service ? ` ${i.compose_service}` : "";
      html += `<p>${place} This container belongs to the Compose project in <code>${esc(i.compose_dir)}</code>:</p>
        ${cmd(run(`cd ${arg(i.compose_dir)} && docker compose pull${svc} && docker compose up -d${svc}`))}
        <p class="hint">If Dockhand created this stack, the folder may only exist inside Dockhand. If the command says
          "no such file or directory", use option A.</p>`;
    } else {
      html += `<p>This container wasn't started with Docker Compose, so it has to be recreated with the same settings.
        Use option A, or recreate it the way you created it. To download the new image first:</p>
        <p>${place}</p>${cmd(run(`docker pull ${i.detail}`))}`;
    }
    html += `<h4>Optional: clean up old images</h4>
      <p>${place} Frees disk space taken by images no container uses anymore.</p>
      ${cmd(run("docker image prune -f"))}`;
    return html;
  }

  function open(item) {
    if (!item) return;
    const body = item.kind === "lxc" ? lxc(item) : item.kind === "host" ? host(item) : docker(item);
    $("modalBody").innerHTML = body + `<p class="hint" style="margin-top:16px">When you're done, click <b>Check now</b> to refresh the dashboard.</p>`;
    $("modal").hidden = false;
    $("modalClose").focus();
  }

  function close() { $("modal").hidden = true; }

  $("modalClose").addEventListener("click", close);
  $("modal").addEventListener("click", async (e) => {
    if (e.target === $("modal")) return close();
    const b = e.target.closest("[data-copy]");
    if (b) {
      const ok = await copyText(b.previousElementSibling.textContent);
      b.textContent = ok ? "Copied!" : "Select & copy";
      setTimeout(() => { b.textContent = "Copy"; }, 2000);
    }
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("modal").hidden) close(); });

  return { open };
})();
