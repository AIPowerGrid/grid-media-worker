const state = { lastLogCount: 0 };

const el = (id) => document.getElementById(id);
const text = (id, value) => { el(id).textContent = value ?? "-"; };
const compactHash = (value) => value ? `${value.slice(0, 12)}...${value.slice(-8)}` : "Pending release";
const compactAddress = (value) => value ? `${value.slice(0, 8)}...${value.slice(-6)}` : "-";
const gib = (mb) => Number.isFinite(mb) ? `${(mb / 1024).toFixed(1)} GiB` : "-";

function badge(id, label, kind = "neutral") {
  const node = el(id);
  node.textContent = label;
  node.className = `badge badge-${kind}`;
}

function step(name, label, mode) {
  text(`${name}-step`, label);
  const node = document.querySelector(`[data-step="${name}"]`);
  node.classList.toggle("complete", mode === "complete");
  node.classList.toggle("active", mode === "active");
}

function render(data) {
  el("connection-state").textContent = "Local connection";
  el("connection-state").classList.add("online");

  const profile = data.profile || {};
  text("profile-version", profile.available ? `${profile.display_name} v${profile.version}` : "Release unavailable");
  text("profile-digest", compactHash(profile.digest));
  text("recipe-root", compactHash(profile.recipe_root));
  if (!profile.available) {
    step("profile", "Unavailable", "active");
  } else if (profile.signature_verified && profile.status === "active") {
    step("profile", "Signed and approved", "complete");
  } else {
    step("profile", "Preview release", "active");
  }

  const hardware = data.hardware || {};
  const gpu = hardware.gpu || {};
  text("gpu-name", gpu.name || "No supported GPU detected");
  text("gpu-vram", gib(gpu.vram_mb));
  text("gpu-driver", gpu.driver || "-");
  text("host-ram", gib(hardware.ram_mb));
  text("host-disk", gib(hardware.disk_free_mb));
  const hardwareKind = hardware.status === "recommended" ? "good" : hardware.status === "supported" ? "info" : "bad";
  badge("hardware-badge", hardware.status || "Unknown", hardwareKind);
  step("hardware", hardware.capability_tier || hardware.status || "Unknown", hardware.status === "unsupported" ? "active" : "complete");
  const notes = [...(hardware.reasons || []), ...(hardware.warnings || [])];
  el("hardware-notes").hidden = notes.length === 0;
  el("hardware-notes").textContent = notes.join(" ");

  const install = data.installation || {};
  const canaryPassed = Boolean(install.canary_passed);
  text("canary-status", canaryPassed ? "Passed" : "Pending");
  if (install.valid && canaryPassed) {
    badge("install-badge", "Validated", "good");
    step("install", "Canary passed", "complete");
  } else if (install.valid) {
    badge("install-badge", "Canary pending", "warn");
    step("install", "Canary pending", "active");
  } else {
    badge("install-badge", "Not installed", "neutral");
    step("install", "Pending", profile.available ? "active" : "");
  }

  const identity = data.identity || {};
  text("worker-signer", identity.worker_signer ? compactAddress(identity.worker_signer) : "Created during setup");
  text("payout-wallet", identity.payout_wallet ? compactAddress(identity.payout_wallet) : "Connect with the Console");
  text("worker-name", identity.worker_name || "-");
  if (identity.connected) {
    badge("identity-badge", "Connected", "good");
    step("identity", "Wallet delegated", "complete");
  } else {
    badge("identity-badge", "Not connected", "neutral");
    step("identity", "Pending", install.valid && canaryPassed ? "active" : "");
  }

  renderProcess(data.process || {});
  configureActions(data);
}

function renderProcess(process) {
  const running = Boolean(process.running);
  text("process-title", running ? `${titleCase(process.action)} in progress` : process.error ? "Action failed" : "Manager idle");
  const dot = el("process-badge");
  dot.className = `pulse-dot${running ? " running" : process.error ? " error" : ""}`;
  dot.setAttribute("aria-label", running ? "Running" : process.error ? "Error" : "Idle");
  el("process-error").hidden = !process.error;
  el("process-error").textContent = process.error || "";

  const logs = process.logs || [];
  const output = el("log-output");
  if (!logs.length) {
    output.innerHTML = '<p class="empty-log">Setup activity will appear here.</p>';
    state.lastLogCount = 0;
    return;
  }
  output.replaceChildren(...logs.map((entry) => {
    const row = document.createElement("div");
    row.className = `log-line ${entry.channel}`;
    const time = document.createElement("time");
    time.textContent = new Date(entry.timestamp * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const message = document.createElement("span");
    message.textContent = entry.message;
    row.append(time, message);
    return row;
  }));
  if (logs.length !== state.lastLogCount) output.scrollTop = output.scrollHeight;
  state.lastLogCount = logs.length;
}

function configureActions(data) {
  const primary = el("primary-action");
  const stop = el("stop-action");
  const running = Boolean(data.process?.running);
  const profile = data.profile || {};
  const install = data.installation || {};
  stop.hidden = !running;
  primary.disabled = running || data.hardware?.status === "unsupported" || !profile.available;

  if (profile.available && (!profile.signature_verified || profile.status !== "active")) {
    if (install.valid) {
      primary.textContent = install.canary_passed ? "Run audio test again" : "Run audio test";
      primary.dataset.action = "canary";
    } else {
      primary.textContent = "Install preview runtime";
      primary.dataset.action = "install";
    }
    return;
  }

  if (data.ready) {
    primary.textContent = "Start worker";
    primary.dataset.action = "serve";
  } else {
    primary.textContent = "Set up worker";
    primary.dataset.action = "setup";
  }
}

async function postAction(action) {
  const response = await fetch("/api/manager/action", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action }),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `Action failed (${response.status})`);
  }
  await poll();
}

async function poll() {
  try {
    const response = await fetch("/api/manager/status", { cache: "no-store" });
    if (!response.ok) throw new Error(`Status failed (${response.status})`);
    render(await response.json());
  } catch (error) {
    el("connection-state").textContent = "Disconnected";
    el("connection-state").classList.remove("online");
  }
}

function titleCase(value) {
  return value ? value.charAt(0).toUpperCase() + value.slice(1) : "Operation";
}

el("primary-action").addEventListener("click", async () => {
  try { await postAction(el("primary-action").dataset.action); }
  catch (error) { el("process-error").hidden = false; el("process-error").textContent = error.message; }
});
el("stop-action").addEventListener("click", async () => {
  try { await postAction("stop"); }
  catch (error) { el("process-error").hidden = false; el("process-error").textContent = error.message; }
});

poll();
setInterval(poll, 2000);
