// Running updates from the app (optional feature) and showing their live output.
const Jobs = (() => {
  const MANAGERS = ["apt", "apk", "dnf", "pacman"];
  let current = null;  // job id shown in the modal
  let timer;

  function canApply(i, action = i.kind === "docker" ? "compose-update" : "upgrade") {
    if (action === "upgrade") return i.kind === "lxc" && i.vmid != null && MANAGERS.includes(i.manager);
    if (action === "reboot") return i.kind === "lxc" && i.vmid != null;
    if (action === "compose-update") return i.kind === "docker" && i.vmid != null && !!i.compose_dir && !!i.container_id;
    return false;
  }

  function confirmText(i, action) {
    if (action === "reboot") return `Restart ${i.name} (CT ${i.vmid}) now?\n\nIts services will be unavailable for a moment.`;
    if (action === "compose-update")
      return `Update ${i.name} now?\n\nThis pulls the new image for ${i.detail} and recreates the container `
        + `(docker compose pull + up -d in ${i.compose_dir}). Data in volumes is kept.`;
    return `Install ${i.update_count} package update(s) on ${i.name} (CT ${i.vmid}) now?\n\n`
      + `Runs a non-interactive upgrade inside the container. Your config files are kept.`;
  }

  async function start(item, action) {
    action = action || (item.kind === "docker" ? "compose-update" : "upgrade");
    if (!confirm(confirmText(item, action))) return;
    try {
      const job = await api("api/jobs", "POST", { id: item.id, action });
      view(job.id);
      Dashboard.load();
    } catch (e) {
      alert(`Could not start: ${e.message}`);
    }
  }

  function view(jobId) {
    current = jobId;
    HowTo.show(`<h3 id="modalTitle">…</h3>
      <p class="lead" id="jobStatus"><span class="spin"></span> Starting…</p>
      <div class="cmd"><pre id="jobOutput" class="job-output"></pre></div>
      <div id="jobActions"></div>`);
    poll();
  }

  async function poll() {
    clearTimeout(timer);
    if (current === null || $("modal").hidden || !$("jobOutput")) { current = null; return; }
    let job;
    try { job = await api(`api/jobs/${current}`); } catch (e) {
      $("jobStatus").textContent = `Lost track of this job: ${e.message}`;
      return;
    }
    $("modalTitle").textContent = job.title;
    const out = $("jobOutput");
    const atBottom = out.scrollHeight - out.scrollTop - out.clientHeight < 30;
    out.textContent = job.output || "";
    if (atBottom) out.scrollTop = out.scrollHeight;

    if (job.status === "running") {
      $("jobStatus").innerHTML = `<span class="spin"></span> Running… you can close this window, it keeps going.`;
      timer = setTimeout(poll, 1000);
      return;
    }
    if (!job.done) {
      $("jobStatus").innerHTML = `<span class="spin"></span> Done, checking the result…`;
      timer = setTimeout(poll, 1500);
      return;
    }
    $("jobStatus").innerHTML = job.status === "succeeded"
      ? `<span class="ok-text">✓ Done.</span> The dashboard has been refreshed.`
      : `<span class="bad-text">✗ Failed${job.exit_code != null ? ` (exit code ${job.exit_code})` : ""}.</span> See the output above.`;
    await Dashboard.load();
    const item = Dashboard.item(job.item_id);
    if (job.status === "succeeded" && job.action === "upgrade" && item && item.reboot_required) {
      $("jobActions").innerHTML = `<p>The container says a restart is needed to finish the update.</p>
        <button class="primary" data-job-reboot="${esc(item.id)}">Restart ${esc(item.name)} now</button>`;
    }
  }

  document.addEventListener("click", (e) => {
    const r = e.target.closest("[data-job-reboot]");
    if (r) { const item = Dashboard.item(r.dataset.jobReboot); if (item) start(item, "reboot"); return; }
    const v = e.target.closest("[data-job-view]");
    if (v) { view(v.dataset.jobView); return; }
    const a = e.target.closest("[data-apply]");
    if (a) { const item = Dashboard.item(a.dataset.apply); if (item) start(item); }
  });

  return { canApply, start, view };
})();
