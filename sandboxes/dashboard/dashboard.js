
const MAX_HISTORY = 4320;
const DEFAULT_REFRESH_INTERVAL_MS = 2000;
const MAX_NODE_ROWS = 250;
const MAX_REGISTRY_REPOSITORY_ROWS = 250;

const state = {
  timer: null,
  currentPage: "overview",
  history: [],
  lastSnapshot: null,
  lastSandboxes: [],
  sandboxFetchInFlight: false,
  sandboxActionInFlight: false,
  terminatingSandboxIds: new Set(),
  metricsRequest: null,
  requestSequence: 0,
  appliedSequence: 0,
  resizeFrame: null,
  lastSandboxRefreshAt: 0,
};

let palette = {
  blue: "#2563eb",
  blueSoft: "rgba(37, 99, 235, 0.12)",
  green: "#16a34a",
  greenSoft: "rgba(22, 163, 74, 0.12)",
  orange: "#f97316",
  orangeSoft: "rgba(249, 115, 22, 0.12)",
  purple: "#7c3aed",
  purpleSoft: "rgba(124, 58, 237, 0.12)",
  red: "#dc2626",
  redSoft: "rgba(220, 38, 38, 0.12)",
  grid: "#dfe5ee",
  text: "#0f172a",
  muted: "#64748b",
  plotBg: "#ffffff",
};

const els = {};

document.addEventListener("DOMContentLoaded", () => {
  for (const id of [
    "connectionStatus",
    "toastRegion",
    "pageKicker",
    "pageHeading",
    "pageDescription",
    "overviewNavBadge",
    "schedulerNavBadge",
    "nodesNavBadge",
    "sandboxesNavBadge",
    "registryNavBadge",
    "lastUpdated",
    "timeRangeSelect",
    "refreshNowButton",
    "themeButton",
    "authToggleButton",
    "authPanel",
    "tokenInput",
    "saveTokenButton",
    "clearTokenButton",
    "activeNodesValue",
    "activeNodesDetail",
    "runningSandboxesValue",
    "runningSandboxesDetail",
    "readyWakeValue",
    "readyWakeDetail",
    "diskCommitValue",
    "diskCommitDetail",
    "modelWaitValue",
    "modelWaitDetail",
    "wakeLatencyValue",
    "wakeLatencyDetail",
    "overviewDecisionTitle",
    "overviewDecisionReasons",
    "overviewSupplyValue",
    "overviewProjectedValue",
    "overviewDeficitValue",
    "capacityFitBadge",
    "capacitySummary",
    "capacityCpuValue",
    "capacityCpuActualMeter",
    "capacityCpuReservedMeter",
    "capacityCpuDetail",
    "capacityMemoryValue",
    "capacityMemoryActualMeter",
    "capacityMemoryReservedMeter",
    "capacityMemoryDetail",
    "capacityDiskValue",
    "capacityDiskMeter",
    "capacityDiskDetail",
    "overviewModelWaitAge",
    "overviewWakingValue",
    "overviewActingValue",
    "overviewModelLatency",
    "builderSummary",
    "builderReadyValue",
    "builderPreparedValue",
    "builderActiveBuildsValue",
    "builderCpuValue",
    "builderMemoryValue",
    "autoscalerSummary",
    "autoscalerProvisioningValue",
    "autoscalerIdleGraceValue",
    "programSummary",
    "programModelWaitValue",
    "programReadyValue",
    "programOldestReadyValue",
    "programWakeLatencyValue",
    "healthBadge",
    "healthTitle",
    "healthDetail",
    "healthSignals",
    "copyDiagnosticsButton",
    "downloadSnapshotButton",
    "schedulerPage",
    "schedulerDecisionTitle",
    "schedulerDecisionDetail",
    "schedulerReasons",
    "schedulerReadyNodesValue",
    "schedulerProvisioningValue",
    "programFlowSummary",
    "flowAllValue",
    "flowModelWaitValue",
    "flowModelWaitDetail",
    "flowReadyValue",
    "flowReadyDetail",
    "flowWakingValue",
    "flowActingValue",
    "decisionPressureValue",
    "decisionIdleGraceValue",
    "equationImmediateCpu",
    "equationImmediateMemory",
    "equationImmediateDisk",
    "equationPreparedCpu",
    "equationPreparedMemory",
    "equationPreparedDisk",
    "equationFreeCpu",
    "equationFreeMemory",
    "equationFreeDisk",
    "equationDeficitCpu",
    "equationDeficitMemory",
    "equationDeficitDisk",
    "policyValues",
    "nodesPage",
    "nodesPageDetail",
    "nodesReadyValue",
    "nodesProvisioningValue",
    "nodesDrainingValue",
    "nodesIncompatibleValue",
    "nodesDiskFreeValue",
    "nodesCpuPressureValue",
    "nodesCpuPressureDetail",
    "nodesMemoryPressureValue",
    "nodesMemoryPressureDetail",
    "nodesPsiValue",
    "nodesStorageQueueValue",
    "nodesStorageQueueDetail",
    "nodesVolumeErrorsValue",
    "nodeSearchInput",
    "nodeStateFilter",
    "nodeTableSummary",
    "nodeRows",
    "sandboxesPage",
    "sandboxesPageStatusBadge",
    "sandboxesPageDetail",
    "sandboxesPageRowsValue",
    "sandboxesPageTerminableValue",
    "sandboxesPagePendingValue",
    "sandboxesPageRoutesValue",
    "sandboxSearchInput",
    "sandboxStateFilter",
    "refreshSandboxesButton",
    "sandboxesPageSummary",
    "sandboxListSummary",
    "sandboxRows",
    "registryPage",
    "registryPageStatusBadge",
    "registryPageUrl",
    "registryPageHealthDetail",
    "registryPageReposValue",
    "registryPageTagsValue",
    "registryPageVisibleTagsValue",
    "registryPageCoverageValue",
    "registryPageDiskValue",
    "registryActiveBuildsValue",
    "registryFailedBuildsValue",
    "registryPendingBuildsValue",
    "registryOldestBuildValue",
    "registryActiveBuildsSummaryValue",
    "registryFailedBuildsSummaryValue",
    "registrySearchInput",
    "registryFilterSelect",
    "registryPageSummary",
    "registryRepoSummary",
    "registryRepoRows",
    "registryTagSummary",
    "registryTagRows",
    "registryBuildSummary",
    "registryBuildRows",
    "buildSummary",
    "buildRows",
    "telemetrySummary",
    "telemetryRows",
    "eventSummary",
    "eventRows",
  ]) {
    els[id] = document.getElementById(id);
  }

  const savedToken = sessionStorage.getItem("ucloud.dashboard.token") || "";
  els.tokenInput.value = savedToken;
  restoreSelectPreference(els.sandboxStateFilter, "ucloud.dashboard.sandboxState");
  restoreSelectPreference(els.nodeStateFilter, "ucloud.dashboard.nodeState");
  restoreSelectPreference(els.registryFilterSelect, "ucloud.dashboard.registryFilter");
  applyTheme(localStorage.getItem("ucloud.dashboard.theme") || preferredTheme());
  document.querySelectorAll("canvas").forEach((canvas) => {
    const heading = canvas.closest("article")?.querySelector("h2, .metric-label");
    const label = `${heading?.textContent?.trim() || "Metric"} session chart`;
    canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", label);
    canvas.textContent = label;
  });
  document.querySelectorAll(".info-dot[title]").forEach((dot) => {
    dot.setAttribute("role", "note");
    dot.setAttribute("aria-label", dot.title);
  });
  syncAuthPanel(!savedToken);
  els.authToggleButton.addEventListener("click", () => syncAuthPanel(els.authPanel.hidden));
  els.saveTokenButton.addEventListener("click", saveToken);
  els.clearTokenButton.addEventListener("click", clearToken);
  els.timeRangeSelect.addEventListener("change", () => {
    trimHistory();
    redrawCharts();
  });
  els.refreshNowButton.addEventListener("click", () => refreshNow({ supersede: true }));
  els.themeButton.addEventListener("click", toggleTheme);
  els.copyDiagnosticsButton.addEventListener("click", copyDiagnostics);
  els.downloadSnapshotButton.addEventListener("click", downloadSnapshot);
  document.querySelectorAll("[data-page-target]").forEach((button) => {
    button.addEventListener("click", () => setPage(button.dataset.pageTarget || "overview"));
    button.addEventListener("keydown", handleTabKeydown);
  });
  window.addEventListener("hashchange", () => setPage(pageFromHash(), { updateHash: false }));
  els.sandboxSearchInput.addEventListener("input", renderSandboxesPage);
  els.sandboxStateFilter.addEventListener("change", () => {
    persistSelectPreference(els.sandboxStateFilter, "ucloud.dashboard.sandboxState");
    renderSandboxesPage();
  });
  els.refreshSandboxesButton.addEventListener("click", () => refreshSandboxes({ force: true }));
  els.registrySearchInput.addEventListener("input", () => renderRegistryPage(state.lastSnapshot || {}));
  els.registryFilterSelect.addEventListener("change", () => {
    persistSelectPreference(els.registryFilterSelect, "ucloud.dashboard.registryFilter");
    renderRegistryPage(state.lastSnapshot || {});
  });
  els.nodeSearchInput.addEventListener("input", renderNodesPage);
  els.nodeStateFilter.addEventListener("change", () => {
    persistSelectPreference(els.nodeStateFilter, "ucloud.dashboard.nodeState");
    renderNodesPage();
  });
  window.addEventListener("resize", scheduleChartRedraw);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      clearRefreshTimer();
      return;
    }
    refreshNow({ supersede: true });
    scheduleNextRefresh();
  });
  setPage(pageFromHash(), { updateHash: false });
  refreshNow();
});

function saveToken() {
  const token = els.tokenInput.value.trim();
  if (token) {
    sessionStorage.setItem("ucloud.dashboard.token", token);
    syncAuthPanel(false);
    setStatus("Saved", "ok");
    refreshNow({ supersede: true });
    return;
  }
  clearToken();
}

function restoreSelectPreference(select, key) {
  if (!select) return;
  const saved = localStorage.getItem(key);
  if (saved !== null && [...select.options].some((option) => option.value === saved)) {
    select.value = saved;
  }
}

function persistSelectPreference(select, key) {
  if (select) localStorage.setItem(key, select.value);
}

function clearToken() {
  sessionStorage.removeItem("ucloud.dashboard.token");
  els.tokenInput.value = "";
  syncAuthPanel(true);
  setStatus("Auth required", "warn");
  els.lastUpdated.textContent = "Enter the gateway bearer token";
}

function syncAuthPanel(show) {
  els.authPanel.hidden = !show;
  els.authToggleButton.setAttribute("aria-expanded", String(show));
}

function preferredTheme() {
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function toggleTheme() {
  applyTheme(document.documentElement.classList.contains("dark") ? "light" : "dark");
  redrawCharts();
}

function applyTheme(theme) {
  const dark = theme === "dark";
  document.documentElement.classList.toggle("dark", dark);
  localStorage.setItem("ucloud.dashboard.theme", dark ? "dark" : "light");
  els.themeButton.title = dark ? "Use light theme" : "Use dark theme";
  els.themeButton.setAttribute("aria-label", els.themeButton.title);
  palette = dark
    ? {
        ...palette,
        blue: "#809bff",
        blueSoft: "rgba(128, 155, 255, 0.10)",
        green: "#5cc7a7",
        greenSoft: "rgba(92, 199, 167, 0.10)",
        orange: "#dfa568",
        orangeSoft: "rgba(223, 165, 104, 0.10)",
        purple: "#ad99ec",
        purpleSoft: "rgba(173, 153, 236, 0.10)",
        red: "#f07d8c",
        redSoft: "rgba(240, 125, 140, 0.10)",
        grid: "#29313e",
        text: "#f0f2f6",
        muted: "#9aa5b5",
        plotBg: "#11151d",
      }
    : {
        ...palette,
        blue: "#4d6ee8",
        blueSoft: "rgba(77, 110, 232, 0.08)",
        green: "#23866f",
        greenSoft: "rgba(35, 134, 111, 0.08)",
        orange: "#a96424",
        orangeSoft: "rgba(169, 100, 36, 0.08)",
        purple: "#7058b3",
        purpleSoft: "rgba(112, 88, 179, 0.08)",
        red: "#c94b5d",
        redSoft: "rgba(201, 75, 93, 0.08)",
        grid: "#d9dee7",
        text: "#171a21",
        muted: "#697386",
        plotBg: "#ffffff",
      };
}

function pageFromHash() {
  const page = window.location.hash.replace(/^#/, "");
  return ["overview", "scheduler", "nodes", "sandboxes", "registry"].includes(page) ? page : "overview";
}

function setPage(page, options = {}) {
  const next = ["overview", "scheduler", "nodes", "sandboxes", "registry"].includes(page) ? page : "overview";
  const pageCopy = {
    overview: ["Control plane", "Overview", "Overview"],
    scheduler: ["Control plane", "Demand & scaling", "Demand & scaling"],
    nodes: ["Control plane", "Fleet", "Fleet"],
    sandboxes: ["Control plane", "Sandboxes", "Sandboxes"],
    registry: ["Control plane", "Images", "Images"],
  };
  state.currentPage = next;
  const [kicker, heading, description] = pageCopy[next];
  setText("pageKicker", kicker);
  setText("pageHeading", heading);
  setText("pageDescription", description);
  const overviewPage = document.getElementById("overviewPage");
  if (overviewPage) overviewPage.hidden = next !== "overview";
  if (els.schedulerPage) els.schedulerPage.hidden = next !== "scheduler";
  if (els.nodesPage) els.nodesPage.hidden = next !== "nodes";
  if (els.sandboxesPage) {
    els.sandboxesPage.hidden = next !== "sandboxes";
  }
  if (els.registryPage) {
    els.registryPage.hidden = next !== "registry";
  }
  document.querySelectorAll("[data-page-target]").forEach((button) => {
    const active = button.dataset.pageTarget === next;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-selected", String(active));
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
    button.tabIndex = active ? 0 : -1;
  });
  if (options.updateHash !== false) {
    const hash = `#${next}`;
    if (window.location.hash !== hash) {
      window.history.replaceState(null, "", hash);
    }
  }
  if (next === "overview") {
    redrawCharts();
    renderOverviewDetail(state.lastSnapshot || {});
  } else if (next === "scheduler") {
    renderSchedulerPage(state.lastSnapshot || {});
  } else if (next === "nodes") {
    renderNodesPage();
  } else if (next === "registry") {
    renderRegistryPage(state.lastSnapshot || {});
  } else {
    renderSandboxesPage();
    refreshSandboxes({ force: true, quiet: true });
  }
}

function handleTabKeydown(event) {
  if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
  const tabs = [...document.querySelectorAll("[data-page-target]")];
  const current = tabs.indexOf(event.currentTarget);
  let index = current;
  if (event.key === "Home") index = 0;
  else if (event.key === "End") index = tabs.length - 1;
  else index = (current + (["ArrowRight", "ArrowDown"].includes(event.key) ? 1 : -1) + tabs.length) % tabs.length;
  event.preventDefault();
  tabs[index].focus();
  setPage(tabs[index].dataset.pageTarget || "overview");
}

function clearRefreshTimer() {
  if (state.timer !== null) {
    window.clearTimeout(state.timer);
    state.timer = null;
  }
}

function scheduleNextRefresh() {
  clearRefreshTimer();
  if (document.hidden) return;
  state.timer = window.setTimeout(() => refreshNow(), DEFAULT_REFRESH_INTERVAL_MS);
}

async function refreshNow(options = {}) {
  if (state.metricsRequest) {
    if (!options.supersede) return;
    state.metricsRequest.abort();
  }
  clearRefreshTimer();
  const controller = new AbortController();
  const sequence = ++state.requestSequence;
  state.metricsRequest = controller;
  els.refreshNowButton.disabled = true;
  if (!state.lastSnapshot) setStatus("Connecting", "warn");
  const token = sessionStorage.getItem("ucloud.dashboard.token") || els.tokenInput.value.trim();
  const headers = token ? { "X-UCloud-Sandbox-Token": token } : {};
  try {
    const response = await fetch("/v1/metrics", {
      headers,
      cache: "no-store",
      signal: controller.signal,
    });
    if (sequence < state.requestSequence) return;
    if (response.status === 401) {
      setStatus("Auth required", "warn");
      els.lastUpdated.textContent = "Enter the gateway bearer token";
      syncAuthPanel(true);
      redrawCharts();
      return;
    }
    if (!response.ok) {
      setStatus(`HTTP ${response.status}`, "bad");
      els.lastUpdated.textContent = state.lastSnapshot
        ? `Last data ${formatTime(state.lastSnapshot.generated_at)}, refresh failed`
        : "Metrics request failed";
      return;
    }
    const snapshot = await response.json();
    if (sequence < state.requestSequence) return;
    state.appliedSequence = sequence;
    setStatus("Live", "ok");
    syncAuthPanel(false);
    renderSnapshot(snapshot);
    if (state.currentPage === "sandboxes") {
      const now = Date.now();
      if (now - state.lastSandboxRefreshAt >= 30000) {
        await refreshSandboxes({ quiet: true });
      }
    }
  } catch (error) {
    if (error && error.name === "AbortError") return;
    setStatus("Offline", "bad");
    els.lastUpdated.textContent = state.lastSnapshot
      ? `Last data ${formatTime(state.lastSnapshot.generated_at)}, connection lost`
      : String(error && error.message ? error.message : error);
    redrawCharts();
  } finally {
    if (state.metricsRequest === controller) {
      state.metricsRequest = null;
      els.refreshNowButton.disabled = false;
      scheduleNextRefresh();
    }
  }
}

function dashboardAuthHeaders() {
  const token = sessionStorage.getItem("ucloud.dashboard.token") || els.tokenInput.value.trim();
  return token ? { "X-UCloud-Sandbox-Token": token } : {};
}

async function dashboardJsonRequest(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    cache: "no-store",
    headers: {
      ...dashboardAuthHeaders(),
      ...(options.headers || {}),
    },
  });
  const contentType = response.headers.get("Content-Type") || "";
  let payload = {};
  if (contentType.includes("application/json")) {
    payload = await response.json();
  } else {
    payload = { error: summarizeResponseText(await response.text()) };
  }
  if (response.status === 401) {
    syncAuthPanel(true);
    throw new Error("Auth required");
  }
  if (!response.ok) {
    const detail = payload && payload.error ? String(payload.error) : `HTTP ${response.status}`;
    throw new Error(detail);
  }
  return payload;
}

function summarizeResponseText(text) {
  const normalized = String(text || "").replace(/\s+/g, " ").trim();
  const titleMatch = normalized.match(/<title>(.*?)<\/title>/i);
  const selected = titleMatch ? titleMatch[1] : normalized;
  if (!selected) return "non-JSON response";
  return selected.length > 300 ? `${selected.slice(0, 300)}...` : selected;
}

function errorMessage(error) {
  return String(error && error.message ? error.message : error);
}

function setStatus(text, mode) {
  els.connectionStatus.textContent = text;
  els.connectionStatus.className = `status-pill status-${mode || "warn"}`;
}

function renderSnapshot(snapshot) {
  state.lastSnapshot = snapshot;
  state.history.push(pointFromSnapshot(snapshot));
  trimHistory();
  els.lastUpdated.textContent = `Updated ${formatTime(snapshot.generated_at)}`;
  renderMetrics(snapshot);
  renderHealth(snapshot);
  if (state.currentPage === "overview") {
    renderOverviewDetail(snapshot);
  } else if (state.currentPage === "scheduler") {
    renderSchedulerPage(snapshot);
  } else if (state.currentPage === "nodes") {
    renderNodesPage();
  } else if (state.currentPage === "registry") {
    renderRegistryPage(snapshot);
  } else {
    renderSandboxesPage();
  }
}

function renderOverviewDetail(snapshot) {
  if (!snapshot || !snapshot.generated_at) return;
  renderBuilds(snapshot);
  renderTelemetry(snapshot);
  renderEvents(snapshot);
  redrawCharts();
}

function trimHistory() {
  const windowMs = Number(els.timeRangeSelect.value) || 3600000;
  const cutoff = Date.now() - windowMs;
  state.history = state.history.filter((point) => point.at >= cutoff);
  if (state.history.length > MAX_HISTORY) {
    state.history.splice(0, state.history.length - MAX_HISTORY);
  }
}

function pointFromSnapshot(snapshot) {
  const nodes = snapshot.nodes || {};
  const resources = snapshot.resources || {};
  const sandboxResources = resources.sandbox || {};
  const actual = sandboxResources.actual_usage || {};
  const load = sandboxResources.load || {};
  const sandboxes = snapshot.sandboxes || {};
  const capacity = snapshot.capacity || {};
  const images = snapshot.images || {};
  const builders = snapshot.builders || {};
  const programs = snapshot.programs || {};
  const programStates = programs.states || {};
  const scale = snapshot.scale_up || {};
  const recentEvents = ((snapshot.events || {}).recent || []);
  const cpuActual = nullableNumber(actual.cpu_percent_avg);
  const memoryActual = nullableNumber(actual.memory_percent);
  const cpuReserved = ratioToPercent(load.vcpu);
  const memoryReserved = ratioToPercent(load.memory);
  return {
    at: Date.parse(snapshot.generated_at) || Date.now(),
    activeNodes: firstNumber(nodes.sandbox_ready, nodes.sandbox) || 0,
    freshNodes: asNumber(nodes.fresh),
    activeSandboxes: asNumber(sandboxes.active_routes),
    builderNodes: asNumber(nodes.builder),
    readyWake: asNumber(programStates.ready_to_wake),
    modelWait: asNumber(programStates.model_wait),
    wakeP95Seconds: msToSeconds(programs.response_to_wake_p95_ms),
    wakeP50Seconds: msToSeconds(programs.response_to_wake_p50_ms),
    modelWaitP95Seconds: msToSeconds(programs.model_wait_p95_ms),
    hardDiskUtilization: resourcePercent(
      sandboxResources.used || {},
      sandboxResources.total || {},
      "disk_mb"
    ),
    pendingSandboxes: asNumber(sandboxes.pending),
    preparedSandboxes: asNumber(capacity.prepared_sandboxes),
    pendingBuilds: asNumber(images.pending_builds),
    activeBuilds: asNumber(images.active_builds),
    preparedBuilders: asNumber(builders.prepared_builders),
    cpuUtilization: firstNumber(cpuActual, cpuReserved),
    cpuReserved,
    memoryUtilization: firstNumber(memoryActual, memoryReserved),
    memoryReserved,
    scaleP50Seconds: msToSeconds(scale.p50_ms),
    scaleP95Seconds: msToSeconds(scale.p95_ms),
    startP50Seconds: msToSeconds(scale.p50_ms),
    errorRate: eventErrorRate(recentEvents),
  };
}

function renderMetrics(snapshot) {
  const latest = state.history[state.history.length - 1] || pointFromSnapshot(snapshot);
  const nodes = snapshot.nodes || {};
  const sandboxes = snapshot.sandboxes || {};
  const capacity = snapshot.capacity || {};
  const exec = snapshot.exec || {};
  const images = snapshot.images || {};
  const builders = snapshot.builders || {};
  const registry = snapshot.registry || {};
  const resources = snapshot.resources || {};
  const sandboxResources = resources.sandbox || {};
  const builderResources = resources.builder || {};
  const actual = sandboxResources.actual_usage || {};
  const load = sandboxResources.load || {};
  const builderActual = builderResources.actual_usage || {};
  const builderLoad = builderResources.load || {};
  const scale = snapshot.scale_up || {};
  const autoscaler = snapshot.autoscaler || {};
  const liveSignals = autoscaler.live_signals || {};
  const programs = snapshot.programs || {};
  const programStates = programs.states || {};
  const sandboxStates = sandboxes.states || {};

  const staleNodes = (Array.isArray(nodes.items) ? nodes.items : []).filter(
    (node) => (node.capabilities || []).includes("sandbox") && (!node.fresh || !node.agent_version_compatible)
  ).length;
  const fleetAttention = staleNodes + asNumber(nodes.sandbox_draining) + asNumber(nodes.incompatible);
  const sandboxAttention = asNumber(sandboxes.pending) + asNumber(sandboxes.stale_routes)
    + asNumber(sandboxStates.failed) + asNumber(sandboxStates.error) + asNumber(sandboxStates.migrating);
  const imageAttention = asNumber(images.failed_builds) + asNumber(images.active_builds);
  setNavBadge("schedulerNavBadge", asNumber(programStates.ready_to_wake), "");
  setNavBadge("nodesNavBadge", fleetAttention, fleetAttention ? "warn" : "");
  setNavBadge("sandboxesNavBadge", sandboxAttention, sandboxAttention ? "warn" : "");
  setNavBadge("registryNavBadge", registry.configured && !registry.ok ? "!" : imageAttention, registry.configured && !registry.ok || asNumber(images.failed_builds) ? "bad" : "");

  setText("activeNodesValue", formatInteger(latest.activeNodes));
  setText(
    "activeNodesDetail",
    `${asNumber(autoscaler.ready_nodes)} ready, ${asNumber(autoscaler.provisioning_nodes)} booting, ${asNumber(autoscaler.unreachable_nodes)} unreachable, ${asNumber(autoscaler.total_nodes || nodes.sandbox)} total`
  );

  setText("runningSandboxesValue", formatInteger(sandboxes.active_routes));
  const staleRouteText = asNumber(sandboxes.stale_routes) > 0
    ? `, ${asNumber(sandboxes.stale_routes)} stale routes`
    : "";
  setText(
    "runningSandboxesDetail",
    `${asNumber(sandboxStates.running)} running, ${asNumber(sandboxStates.parked)} parked, ${asNumber(sandboxStates.waking)} waking${staleRouteText}`
  );

  setText("readyWakeValue", formatInteger(programStates.ready_to_wake));
  setText(
    "readyWakeDetail",
    asNumber(programStates.ready_to_wake) > 0
      ? `${formatAge(programs.oldest_ready_to_wake_seconds)} oldest`
      : "no response-ready work"
  );

  setText("diskCommitValue", formatPercentPoint(latest.hardDiskUtilization));
  setText(
    "diskCommitDetail",
    `${formatMemory((sandboxResources.free || {}).disk_mb)} free of ${formatMemory((sandboxResources.total || {}).disk_mb)}`
  );

  setText("modelWaitValue", formatInteger(programStates.model_wait));
  setText(
    "modelWaitDetail",
    `${formatResources((programs.resources || {}).model_wait || {})}, ${formatAge(programs.oldest_model_wait_seconds)} oldest`
  );

  setText(
    "wakeLatencyValue",
    latest.wakeP95Seconds === null ? "-" : `${latest.wakeP95Seconds.toFixed(2)}s`
  );
  setText(
    "wakeLatencyDetail",
    `p50 ${formatDurationMs(programs.response_to_wake_p50_ms)}, ${formatInteger(programs.requests)} requests`
  );

  const actions = Array.isArray(autoscaler.actions) ? autoscaler.actions : [];
  const actionText = actionSummary(actions);
  setText(
    "autoscalerSummary",
    autoscaler.timestamp ? `${actionText}, cycle ${formatTime(autoscaler.timestamp)}` : "No autoscaler cycle loaded"
  );
  setText(
    "autoscalerProvisioningValue",
    nullableNumber(liveSignals.provisioning_p95_seconds) === null
      ? "-"
      : `${Number(liveSignals.provisioning_p95_seconds).toFixed(1)}s`
  );
  setText(
    "autoscalerIdleGraceValue",
    nullableNumber(autoscaler.effective_scale_down_idle_seconds) === null
      ? "-"
      : formatAge(autoscaler.effective_scale_down_idle_seconds)
  );
  setText(
    "programSummary",
    `${asNumber(programs.rollouts)} rollouts, ${asNumber(programs.requests)} requests`
  );
  setText("programModelWaitValue", formatInteger(programStates.model_wait));
  setText("programReadyValue", formatInteger(programStates.ready_to_wake));
  setText(
    "programOldestReadyValue",
    asNumber(programStates.ready_to_wake) > 0
      ? formatAge(programs.oldest_ready_to_wake_seconds)
      : "-"
  );
  setText(
    "programWakeLatencyValue",
    asNumber(programs.response_to_wake_p95_ms) > 0
      ? `${(asNumber(programs.response_to_wake_p95_ms) / 1000).toFixed(2)}s`
      : "-"
  );

  setText("builderReadyValue", formatInteger(nodes.builder));
  setText("builderPreparedValue", formatInteger(builders.prepared_builders));
  setText("builderActiveBuildsValue", formatInteger(images.active_builds));
  setText("builderCpuValue", formatPercentPoint(firstNumber(builderActual.cpu_percent_avg, ratioToPercent(builderLoad.vcpu))));
  setText("builderMemoryValue", formatPercentPoint(firstNumber(builderActual.memory_percent, ratioToPercent(builderLoad.memory))));
  const oldestBuildWait = asNumber(images.pending_builds) > 0
    ? formatAge(images.oldest_pending_build_seconds)
    : "none";
  setText(
    "builderSummary",
    `${asNumber(images.pending_builds)} waiting, ${oldestBuildWait} oldest wait, ${asNumber(images.failed_builds)} failed`
  );
  renderOverviewOperational(snapshot);
}

function renderOverviewOperational(snapshot) {
  const nodes = snapshot.nodes || {};
  const sandboxes = snapshot.sandboxes || {};
  const resources = ((snapshot.resources || {}).sandbox || {});
  const actual = resources.actual_usage || {};
  const load = resources.load || {};
  const free = resources.free || {};
  const used = resources.used || {};
  const total = resources.total || {};
  const autoscaler = snapshot.autoscaler || {};
  const policy = autoscaler.effective_policy || {};
  const programs = snapshot.programs || {};
  const states = programs.states || {};
  const actions = Array.isArray(autoscaler.actions) ? autoscaler.actions : [];
  const reasons = Array.isArray(autoscaler.reasons) ? autoscaler.reasons : [];
  const creates = actionCount(actions, "create");
  const stops = actionCount(actions, "stop");
  const decision = creates > 0 ? `Create ${formatInteger(creates)} node${creates === 1 ? "" : "s"}`
    : stops > 0 ? `Stop ${formatInteger(stops)} node${stops === 1 ? "" : "s"}`
      : "Hold current capacity";

  setText("overviewDecisionTitle", decision);
  setText("overviewSupplyValue", `${formatInteger(autoscaler.ready_nodes)} / ${formatInteger(autoscaler.provisioning_nodes)} / ${formatInteger(autoscaler.unreachable_nodes)}`);
  setText("overviewProjectedValue", formatResources(autoscaler.projected_free_resources || {}));
  setText("overviewDeficitValue", formatResources(autoscaler.resource_deficit || {}));
  els.overviewDecisionReasons.replaceChildren(...(reasons.length ? reasons : ["No additional scale action is required."]).slice(0, 3).map((reason) => {
    const item = document.createElement("span");
    item.textContent = String(reason);
    return item;
  }));

  const cpuActual = nullableNumber(actual.cpu_percent_avg);
  const cpuReserved = ratioToPercent(load.vcpu);
  const memoryActual = nullableNumber(actual.memory_percent);
  const memoryReserved = ratioToPercent(load.memory);
  const diskPercent = resourcePercent(used, total, "disk_mb");
  const hasReadySupply = asNumber(nodes.sandbox_ready) > 0;
  const hasDeficit = resourceHasPositiveValue(autoscaler.resource_deficit);
  els.capacityFitBadge.textContent = hasDeficit ? "Does not fit" : hasReadySupply ? "Fits current supply" : "No ready nodes";
  els.capacityFitBadge.className = `inline-badge ${hasDeficit ? "badge-bad" : hasReadySupply ? "badge-ok" : "badge-muted"}`;
  setText(
    "capacitySummary",
    hasDeficit
      ? `Uncovered immediate demand: ${formatResources(autoscaler.resource_deficit || {})}.`
      : `${formatResources(free)} remains across ${formatInteger(nodes.sandbox_ready)} schedulable node(s).`
  );
  setText("capacityCpuValue", `${formatNumber(free.vcpu)} vCPU free`);
  setText("capacityCpuDetail", `Actual ${formatPercentPoint(cpuActual)} / reserved ${formatPercentPoint(cpuReserved)} / target ${formatPercentPoint(ratioToPercent(policy.target_cpu_utilization))}`);
  setMeterWidth("capacityCpuActualMeter", cpuActual);
  setMeterWidth("capacityCpuReservedMeter", cpuReserved);
  setText("capacityMemoryValue", `${formatMemory(free.memory_mb)} free`);
  setText("capacityMemoryDetail", `Actual ${formatPercentPoint(memoryActual)} / reserved ${formatPercentPoint(memoryReserved)} / target ${formatPercentPoint(ratioToPercent(policy.target_memory_utilization))}`);
  setMeterWidth("capacityMemoryActualMeter", memoryActual);
  setMeterWidth("capacityMemoryReservedMeter", memoryReserved);
  setText("capacityDiskValue", `${formatMemory(free.disk_mb)} free`);
  setText("capacityDiskDetail", `${formatMemory(used.disk_mb)} committed of ${formatMemory(total.disk_mb)} hard capacity`);
  setMeterWidth("capacityDiskMeter", diskPercent);

  setText("overviewModelWaitAge", asNumber(states.model_wait) ? `oldest ${formatAge(programs.oldest_model_wait_seconds)}` : "No active wait");
  setText("overviewWakingValue", formatInteger(states.waking));
  setText("overviewActingValue", formatInteger(states.acting));
  setText("overviewModelLatency", formatDurationMs(programs.model_wait_p95_ms));
}

function renderHealth(snapshot) {
  const nodes = snapshot.nodes || {};
  const sandboxes = snapshot.sandboxes || {};
  const images = snapshot.images || {};
  const programs = snapshot.programs || {};
  const states = programs.states || {};
  const autoscaler = snapshot.autoscaler || {};
  const execution = autoscaler.execution || {};
  const scaleDown = execution.scale_down || {};
  const blockedStopJobs = Array.isArray(scaleDown.blocked_job_ids) ? scaleDown.blocked_job_ids : [];
  const registry = snapshot.registry || {};
  const recent = ((snapshot.events || {}).recent || []);
  const volumeErrors = (Array.isArray(nodes.items) ? nodes.items : []).reduce(
    (total, node) => total + asNumber((node.actual_usage || {}).storage_error_volumes),
    0
  );
  const signals = [];
  let severity = "ok";
  let title = "Service is healthy";
  let detail = "Current hard demand fits projected ready capacity.";

  const projectionErrors = recent.filter((event) => event.kind === "program_state_projection_error").length;
  if (projectionErrors > 0) {
    severity = "bad";
    title = "Program state telemetry is degraded";
    detail = `${projectionErrors} recent program state projection error(s) need attention.`;
  } else if (resourceHasPositiveValue(autoscaler.resource_deficit)) {
    severity = "warn";
    title = "Capacity is catching up";
    detail = `Autoscaler deficit: ${formatResources(autoscaler.resource_deficit)}.`;
  } else if (asNumber(sandboxes.stale_routes) > 0 || asNumber(nodes.incompatible) > 0) {
    severity = "warn";
    title = "Some supply is unavailable";
    detail = `${formatInteger(sandboxes.stale_routes)} stale route(s), ${formatInteger(nodes.incompatible)} incompatible node(s).`;
  } else if (blockedStopJobs.length > 0) {
    severity = "warn";
    title = "Scale-down is blocked";
    detail = `${formatInteger(blockedStopJobs.length)} node stop(s) are blocked; inspect the scheduler execution details.`;
  } else if (volumeErrors > 0 || asNumber(images.failed_builds) > 0 || (registry.configured && !registry.ok)) {
    severity = "warn";
    title = "A supporting subsystem needs attention";
    detail = `${formatInteger(volumeErrors)} volume error(s), ${formatInteger(images.failed_builds)} failed build(s)${registry.configured && !registry.ok ? ", registry unavailable" : ""}.`;
  } else if (Boolean(autoscaler.create_pressure_scale_up)) {
    severity = "warn";
    title = "Create pipeline is saturated";
    const live = autoscaler.live_signals || {};
    detail = `${formatInteger(live.sandbox_create_limit)} create slots occupied; ${formatInteger(live.sandbox_create_rejections)} recent rejection(s).`;
  } else if (Boolean(autoscaler.pressure_scale_up)) {
    severity = "warn";
    title = "Live pressure is above policy";
    detail = "The autoscaler is adding or evaluating capacity from live node pressure.";
  } else if (actionCount(autoscaler.actions, "create") > 0 || actionCount(autoscaler.actions, "stop") > 0) {
    title = "Capacity is changing";
    detail = actionSummary(autoscaler.actions);
  }

  if (asNumber(states.ready_to_wake) > 0) {
    signals.push({ text: `${formatInteger(states.ready_to_wake)} ready`, mode: "", page: "scheduler" });
  }
  if (asNumber(states.model_wait) > 0) {
    signals.push({ text: `${formatInteger(states.model_wait)} model wait`, mode: "", page: "scheduler" });
  }
  if (asNumber(nodes.sandbox_draining) > 0) {
    signals.push({ text: `${formatInteger(nodes.sandbox_draining)} draining`, mode: "warn", page: "nodes" });
  }
  if (blockedStopJobs.length > 0) {
    signals.push({ text: `${formatInteger(blockedStopJobs.length)} stop blocked`, mode: "warn", page: "scheduler" });
  }
  if (asNumber(images.failed_builds) > 0) {
    signals.push({ text: `${formatInteger(images.failed_builds)} failed builds`, mode: "warn", page: "registry" });
  }
  if (volumeErrors > 0) {
    signals.push({ text: `${formatInteger(volumeErrors)} volume errors`, mode: "bad", page: "nodes" });
  }
  if (signals.length === 0) signals.push({ text: "No active warnings", mode: "" });

  els.healthBadge.className = `health-icon health-${severity}`;
  els.overviewNavBadge.textContent = severity === "bad" ? "Fix" : severity === "warn" ? "Watch" : "Live";
  els.overviewNavBadge.className = `nav-badge nav-badge-${severity}`;
  setText("healthTitle", title);
  setText("healthDetail", detail);
  els.healthSignals.replaceChildren(...signals.map((signal) => {
    const element = document.createElement(signal.page ? "button" : "span");
    element.className = `signal-chip ${signal.mode}`.trim();
    element.textContent = signal.text;
    if (signal.page) {
      element.type = "button";
      element.title = `Open ${signal.page}`;
      element.addEventListener("click", () => setPage(signal.page));
    }
    return element;
  }));
}

function renderSchedulerPage(snapshot) {
  if (!snapshot || !snapshot.generated_at) return;
  const autoscaler = snapshot.autoscaler || {};
  const programs = snapshot.programs || {};
  const states = programs.states || {};
  const policy = autoscaler.effective_policy || {};
  const actions = Array.isArray(autoscaler.actions) ? autoscaler.actions : [];
  const reasons = Array.isArray(autoscaler.reasons) ? autoscaler.reasons : [];
  const execution = autoscaler.execution || {};
  const scaleDown = execution.scale_down || {};
  const creates = actionCount(actions, "create");
  const stops = actionCount(actions, "stop");
  const decisionTitle = creates > 0
    ? `Create ${formatInteger(creates)} node${creates === 1 ? "" : "s"}`
    : stops > 0
      ? `Stop ${formatInteger(stops)} node${stops === 1 ? "" : "s"}`
      : "Hold current capacity";

  setText("schedulerDecisionTitle", decisionTitle);
  setText(
    "schedulerDecisionDetail",
    autoscaler.timestamp
      ? `${actionSummary(actions)}, cycle ${formatTime(autoscaler.timestamp)}; stops requested ${formatInteger((scaleDown.requested_job_ids || []).length)}, draining ${formatInteger((scaleDown.draining_job_ids || []).length)}, blocked ${formatInteger((scaleDown.blocked_job_ids || []).length)}, terminated ${formatInteger((scaleDown.terminated_job_ids || []).length)}`
      : "No autoscaler cycle loaded."
  );
  els.schedulerReasons.replaceChildren(...(reasons.length ? reasons : ["No scale action is required."]).slice(0, 6).map((reason) => {
    const span = document.createElement("span");
    span.className = `reason-chip ${resourceHasPositiveValue(autoscaler.resource_deficit) ? "warn" : ""}`.trim();
    span.textContent = String(reason);
    return span;
  }));
  setText("schedulerReadyNodesValue", formatInteger(autoscaler.ready_nodes));
  setText("schedulerProvisioningValue", formatInteger(autoscaler.provisioning_nodes));

  setText("flowAllValue", formatInteger(programs.requests));
  setText("flowModelWaitValue", formatInteger(states.model_wait));
  setText("flowModelWaitDetail", `p95 ${formatDurationMs(programs.model_wait_p95_ms)}`);
  setText("flowReadyValue", formatInteger(states.ready_to_wake));
  setText("flowReadyDetail", `oldest ${formatAge(programs.oldest_ready_to_wake_seconds)}`);
  setText("flowWakingValue", formatInteger(states.waking));
  setText("flowActingValue", formatInteger(states.acting));
  setText(
    "programFlowSummary",
    `${formatInteger(programs.rollouts)} rollouts / ${formatInteger(programs.sandboxes)} sandboxes`
  );

  renderCapacityEquation({
    immediate: autoscaler.pending_resources || {},
    prepared: autoscaler.prepared_resources || {},
    free: autoscaler.projected_free_resources || {},
    deficit: autoscaler.resource_deficit || {},
  });
  const liveSignals = autoscaler.live_signals || {};
  const pressureParts = [
    `${formatInteger(liveSignals.pressure_samples)} pressure of ${formatInteger(liveSignals.observation_samples)} observed`,
    `${formatInteger(liveSignals.create_pressure_samples)} create`,
  ];
  if (nullableNumber(liveSignals.cpu_utilization) !== null) {
    pressureParts.push(`CPU ${formatPercentPoint(ratioToPercent(liveSignals.cpu_utilization))}`);
  }
  if (nullableNumber(liveSignals.memory_utilization) !== null) {
    pressureParts.push(`memory ${formatPercentPoint(ratioToPercent(liveSignals.memory_utilization))}`);
  }
  if (nullableNumber(liveSignals.image_materialization_queue_utilization) !== null) {
    pressureParts.push(`${formatPercentPoint(ratioToPercent(liveSignals.image_materialization_queue_utilization))} image materialization`);
  }
  setText("decisionPressureValue", pressureParts.join(" / "));
  setText("decisionIdleGraceValue", `Idle grace ${formatAge(autoscaler.effective_scale_down_idle_seconds)}`);
  renderPolicy(policy);
}

function renderCapacityEquation(rows) {
  const mapping = [
    ["Immediate", rows.immediate],
    ["Prepared", rows.prepared],
    ["Free", rows.free],
    ["Deficit", rows.deficit],
  ];
  for (const [prefix, resources] of mapping) {
    const values = resources || {};
    setText(`equation${prefix}Cpu`, `${formatNumber(values.vcpu)} vCPU`);
    setText(`equation${prefix}Memory`, formatMemory(values.memory_mb));
    setText(`equation${prefix}Disk`, formatMemory(values.disk_mb));
  }
}

function renderPolicy(policy) {
  const rows = [
    ["Node range", `${formatInteger(policy.min_nodes)}–${formatInteger(policy.max_nodes)}`],
    ["Create limits", `${formatInteger(policy.max_create_per_cycle)} per cycle / ${formatInteger(policy.max_provisioning_nodes)} provisioning`],
    ["Stop limit", `${formatInteger(policy.max_stop_per_cycle)} per cycle`],
    ["CPU target", formatPercentPoint(ratioToPercent(policy.target_cpu_utilization))],
    ["Memory target", formatPercentPoint(ratioToPercent(policy.target_memory_utilization))],
    ["Storage queue", formatPercentPoint(ratioToPercent(policy.target_storage_queue_utilization))],
    ["Create target", `${formatInteger(policy.create_target_concurrency_per_node)} per node`],
    ["Create burst", `${formatInteger(policy.create_pressure_max_headroom_nodes)} node max`],
    ["Idle grace", formatAge(policy.scale_down_idle_seconds)],
    ["Pressure cooldown", formatAge(policy.pressure_scale_down_cooldown_seconds)],
    ["Unreachable eviction", formatAge(policy.unreachable_stop_after_seconds)],
    ["Heartbeat TTL", formatAge(policy.heartbeat_ttl_seconds)],
  ];
  els.policyValues.replaceChildren(...rows.map(([name, value]) => {
    const wrapper = document.createElement("div");
    const dt = document.createElement("dt");
    const dd = document.createElement("dd");
    dt.textContent = name;
    dd.textContent = value;
    wrapper.append(dt, dd);
    return wrapper;
  }));
}

function renderNodesPage() {
  if (!els.nodeRows) return;
  const snapshot = state.lastSnapshot || {};
  const nodes = snapshot.nodes || {};
  const resources = (snapshot.resources || {}).sandbox || {};
  const autoscaler = snapshot.autoscaler || {};
  const policy = autoscaler.effective_policy || {};
  const query = String(els.nodeSearchInput.value || "").trim().toLowerCase();
  const filter = String(els.nodeStateFilter.value || "all");
  let items = Array.isArray(nodes.items) ? nodes.items.filter((item) => (item.capabilities || []).includes("sandbox")) : [];
  items = items.filter((item) => {
    const stateName = nodeState(item);
    if (query && !`${item.node_id || ""} ${item.job_id || ""}`.toLowerCase().includes(query)) return false;
    if (filter === "all") return true;
    if (filter === "ready") return stateName.mode === "ok";
    if (filter === "constrained") return nodeConstrained(item);
    if (filter === "draining") return Boolean(item.draining || item.admission_open === false);
    if (filter === "stale") return !item.fresh || !item.agent_version_compatible;
    return true;
  });
  items.sort((a, b) => nodeStateRank(a) - nodeStateRank(b) || asNumber(b.active_workloads) - asNumber(a.active_workloads) || String(a.node_id).localeCompare(String(b.node_id)));
  const shown = items.slice(0, MAX_NODE_ROWS);
  const allSandboxNodes = Array.isArray(nodes.items) ? nodes.items.filter((item) => (item.capabilities || []).includes("sandbox")) : [];
  const freshSandboxNodes = allSandboxNodes.filter((item) => item.fresh && item.agent_version_compatible);
  const aggregateActual = resources.actual_usage || {};
  const aggregateLoad = resources.load || {};
  const storage = freshSandboxNodes.reduce((summary, item) => {
    const actual = item.actual_usage || {};
    summary.active += asNumber(actual.storage_active_operations);
    summary.waiting += asNumber(actual.storage_waiting_operations);
    summary.limit += asNumber(actual.storage_max_concurrent_operations);
    summary.publicationActive += asNumber(actual.storage_publication_active);
    summary.publicationWaiting += asNumber(actual.storage_publication_waiting);
    summary.publicationLimit += asNumber(actual.storage_publication_limit);
    summary.publicationWaitMax = Math.max(summary.publicationWaitMax, asNumber(actual.storage_publication_queue_wait_ms_max));
    summary.publicationDurationMax = Math.max(summary.publicationDurationMax, asNumber(actual.storage_publication_duration_ms_max));
    summary.publications += asNumber(actual.storage_snapshot_publications);
    summary.compactions += asNumber(actual.storage_snapshot_compactions);
    summary.errors += asNumber(actual.storage_error_volumes);
    summary.materializationActive += asNumber(actual.image_materialization_active_operations);
    summary.materializationWaiting += asNumber(actual.image_materialization_waiting_operations);
    summary.materializationLimit += asNumber(actual.image_materialization_max_concurrent_operations);
    summary.psi = Math.max(summary.psi, asNumber(actual.memory_psi_full_avg10));
    return summary;
  }, { active: 0, waiting: 0, limit: 0, publicationActive: 0, publicationWaiting: 0, publicationLimit: 0, publicationWaitMax: 0, publicationDurationMax: 0, publications: 0, compactions: 0, errors: 0, materializationActive: 0, materializationWaiting: 0, materializationLimit: 0, psi: 0 });
  const staleOrIncompatible = allSandboxNodes.filter((item) => !item.fresh || !item.agent_version_compatible).length;
  setText("nodesReadyValue", formatInteger(nodes.sandbox_ready));
  setText("nodesProvisioningValue", formatInteger(autoscaler.provisioning_nodes));
  setText("nodesDrainingValue", formatInteger(nodes.sandbox_draining));
  setText("nodesIncompatibleValue", formatInteger(staleOrIncompatible));
  setText("nodesDiskFreeValue", formatMemory((resources.free || {}).disk_mb));
  setText("nodesCpuPressureValue", `${formatPercentPoint(aggregateActual.cpu_percent_avg)} / ${formatPercentPoint(ratioToPercent(aggregateLoad.vcpu))}`);
  setText("nodesCpuPressureDetail", `target ${formatPercentPoint(ratioToPercent(policy.target_cpu_utilization))}`);
  setText("nodesMemoryPressureValue", `${formatPercentPoint(aggregateActual.memory_percent)} / ${formatPercentPoint(ratioToPercent(aggregateLoad.memory))}`);
  setText("nodesMemoryPressureDetail", `target ${formatPercentPoint(ratioToPercent(policy.target_memory_utilization))}`);
  setText("nodesPsiValue", `${formatNumber(storage.psi)}%`);
  setText("nodesStorageQueueValue", `${formatInteger(storage.active + storage.materializationActive)} / ${formatInteger(storage.waiting + storage.materializationWaiting)}`);
  setText("nodesStorageQueueDetail", `active / waiting; storage ${formatInteger(storage.limit)} max, snapshot ${formatInteger(storage.publicationActive)} / ${formatInteger(storage.publicationWaiting)} of ${formatInteger(storage.publicationLimit)}, max wait ${formatDurationMs(storage.publicationWaitMax)}, max duration ${formatDurationMs(storage.publicationDurationMax)}, ${formatInteger(storage.publications)} published / ${formatInteger(storage.compactions)} compacted, image materialization ${formatInteger(storage.materializationLimit)} max`);
  setText("nodesVolumeErrorsValue", formatInteger(storage.errors));
  setText(
    "nodesPageDetail",
    `${formatInteger(nodes.sandbox_ready)} ready of ${formatInteger(nodes.sandbox)} fresh sandbox nodes.`
  );
  setText("nodeTableSummary", `${formatInteger(shown.length)} shown of ${formatInteger(items.length)} matching`);
  if (shown.length === 0) {
    renderEmptyRow(els.nodeRows, 8, "No nodes match the current filter");
    return;
  }
  els.nodeRows.replaceChildren(...shown.map((item) => nodeRow(item)));
}

function nodeRow(item) {
  const tr = document.createElement("tr");
  const stateInfo = nodeState(item);
  const stateCell = document.createElement("td");
  const stateWrapper = document.createElement("span");
  const dot = document.createElement("i");
  dot.className = `state-dot ${stateInfo.mode}`;
  stateWrapper.className = "state-cell";
  stateWrapper.append(dot, document.createTextNode(stateInfo.label));
  stateCell.append(stateWrapper);
  tr.append(stateCell);
  appendClassCell(tr, `${item.node_id || "-"}\n${item.job_id || "-"}`, "", item.node_url || "");
  appendCell(tr, `${formatInteger(item.active_sandboxes)} sandboxes`);
  tr.append(resourceMeterCell(item, "vcpu", "cpu_percent"));
  tr.append(resourceMeterCell(item, "memory_mb", "memory_percent"));
  const free = item.free_resources || {};
  const total = item.total_resources || {};
  appendCell(tr, `${formatMemory(free.disk_mb)} / ${formatMemory(total.disk_mb)}`);
  appendCell(tr, nodePressureText(item));
  appendCell(tr, item.fresh ? `${formatAge(item.age_seconds)} ago` : `stale ${formatAge(item.age_seconds)}`);
  return tr;
}

function resourceMeterCell(item, resource, actualKey) {
  const td = document.createElement("td");
  const load = ratioToPercent((item.load || {})[resource === "vcpu" ? "vcpu" : "memory"]);
  const actual = nullableNumber((item.actual_usage || {})[actualKey]);
  const value = firstNumber(actual, load, 0);
  const wrapper = document.createElement("div");
  const label = document.createElement("div");
  const meter = document.createElement("div");
  const fill = document.createElement("span");
  const actualLabel = document.createElement("span");
  const reservedLabel = document.createElement("span");
  wrapper.className = "meter-stack";
  label.className = "meter-label";
  actualLabel.textContent = `${formatPercentPoint(actual)} actual`;
  reservedLabel.textContent = `${formatPercentPoint(load)} reserved`;
  label.append(actualLabel, reservedLabel);
  meter.className = `meter ${value >= 90 ? "bad" : value >= 75 ? "warn" : ""}`.trim();
  fill.style.width = `${Math.max(0, Math.min(100, value))}%`;
  meter.append(fill);
  wrapper.append(label, meter);
  td.append(wrapper);
  return td;
}

function nodeState(item) {
  if (!item.fresh) return { label: "Stale", mode: "bad" };
  if (!item.agent_version_compatible) return { label: "Incompatible", mode: "bad" };
  if (item.draining) return { label: "Draining", mode: "warn" };
  if (item.admission_open === false) return { label: "Closed", mode: "warn" };
  if (nodeConstrained(item)) return { label: "Constrained", mode: "warn" };
  return { label: "Ready", mode: "ok" };
}

function nodeStateRank(item) {
  const mode = nodeState(item).mode;
  return mode === "bad" ? 0 : mode === "warn" ? 1 : 2;
}

function nodeConstrained(item) {
  const actual = item.actual_usage || {};
  const load = item.load || {};
  const storageLimit = asNumber(actual.storage_max_concurrent_operations);
  const storageQueue = storageLimit > 0
    ? (asNumber(actual.storage_active_operations) + asNumber(actual.storage_waiting_operations)) / storageLimit
    : 0;
  const publicationLimit = asNumber(actual.storage_publication_limit);
  const publicationQueue = publicationLimit > 0
    ? (asNumber(actual.storage_publication_active) + asNumber(actual.storage_publication_waiting)) / publicationLimit
    : 0;
  const materializationLimit = asNumber(actual.image_materialization_max_concurrent_operations);
  const materializationQueue = materializationLimit > 0
    ? (asNumber(actual.image_materialization_active_operations) + asNumber(actual.image_materialization_waiting_operations)) / materializationLimit
    : 0;
  return asNumber(load.vcpu) >= 0.8
    || asNumber(load.memory) >= 0.85
    || asNumber(actual.memory_psi_full_avg10) >= 5
    || storageQueue >= 0.75
    || publicationQueue >= 0.75
    || materializationQueue >= 0.75
    || asNumber(actual.storage_error_volumes) > 0;
}

function nodePressureText(item) {
  const actual = item.actual_usage || {};
  const parts = [];
  if (nullableNumber(actual.memory_psi_full_avg10) !== null) parts.push(`PSI ${formatNumber(actual.memory_psi_full_avg10)}`);
  const limit = asNumber(actual.storage_max_concurrent_operations);
  if (limit > 0) parts.push(`storage ${asNumber(actual.storage_active_operations) + asNumber(actual.storage_waiting_operations)}/${limit}`);
  const publicationLimit = asNumber(actual.storage_publication_limit);
  if (publicationLimit > 0) parts.push(`snapshot publication ${asNumber(actual.storage_publication_active) + asNumber(actual.storage_publication_waiting)}/${publicationLimit}`);
  if (asNumber(actual.storage_publication_queue_wait_ms_max) > 0) parts.push(`snapshot max wait ${formatDurationMs(actual.storage_publication_queue_wait_ms_max)}`);
  if (asNumber(actual.storage_publication_duration_ms_max) > 0) parts.push(`snapshot max duration ${formatDurationMs(actual.storage_publication_duration_ms_max)}`);
  const materializationLimit = asNumber(actual.image_materialization_max_concurrent_operations);
  if (materializationLimit > 0) parts.push(`image materialization ${asNumber(actual.image_materialization_active_operations) + asNumber(actual.image_materialization_waiting_operations)}/${materializationLimit}`);
  if (asNumber(actual.storage_error_volumes) > 0) parts.push(`${formatInteger(actual.storage_error_volumes)} volume errors`);
  return parts.join(", ") || "normal";
}

function actionCount(actions, kind) {
  return (Array.isArray(actions) ? actions : []).reduce(
    (total, action) => total + (actionKind(action) === kind ? Math.max(0, asNumber((action || {}).count || ((action || {}).job_ids || []).length || 1)) : 0),
    0
  );
}

function actionSummary(actions) {
  if (!Array.isArray(actions) || actions.length === 0) return "Hold";
  return actions.map((action) => {
    const kind = actionKind(action);
    const count = Math.max(0, asNumber((action || {}).count || ((action || {}).job_ids || []).length));
    const label = kind === "create" || kind === "scale_up" ? "Create"
      : kind === "stop" || kind === "scale_down" ? "Stop"
        : kind === "scale_up_builder" ? "Create builder"
          : String(kind || "Action").replaceAll("_", " ");
    return `${label}${count ? ` ${formatInteger(count)}` : ""}`;
  }).join(", ");
}

function actionKind(action) {
  if (typeof action === "string") return action;
  return action && typeof action === "object" ? String(action.kind || "") : "";
}

async function copyDiagnostics() {
  if (!state.lastSnapshot) return;
  const snapshot = state.lastSnapshot;
  const autoscaler = snapshot.autoscaler || {};
  const programs = snapshot.programs || {};
  const text = [
    `Generated: ${snapshot.generated_at || "-"}`,
    `Health: ${els.healthTitle.textContent}`,
    `Nodes: ${asNumber((snapshot.nodes || {}).sandbox_ready)} ready / ${asNumber(autoscaler.provisioning_nodes)} provisioning`,
    `Programs: ${asNumber(programs.requests)} requests, ${asNumber((programs.states || {}).ready_to_wake)} ready`,
    `Decision: ${actionSummary(autoscaler.actions)}`,
    `Reasons: ${(autoscaler.reasons || []).join("; ") || "none"}`,
  ].join("\n");
  try {
    await navigator.clipboard.writeText(text);
    showToast("Operational summary copied");
  } catch (_error) {
    showToast("Could not copy the operational summary", "bad");
  }
}

function downloadSnapshot() {
  if (!state.lastSnapshot) return;
  const blob = new Blob([JSON.stringify(state.lastSnapshot, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `ucloud-sandbox-metrics-${new Date().toISOString().replace(/[:.]/g, "-")}.json`;
  link.click();
  URL.revokeObjectURL(url);
  showToast("Metrics snapshot downloaded");
}

function showToast(message, mode = "ok") {
  if (!els.toastRegion) return;
  els.toastRegion.textContent = message;
  els.toastRegion.className = `toast-region is-visible ${mode === "bad" ? "bad" : ""}`.trim();
  window.setTimeout(() => {
    els.toastRegion.className = "toast-region";
  }, 2400);
}

function renderEvents(snapshot) {
  const allEvents = ((snapshot.events || {}).recent || []).slice().reverse();
  const events = allEvents.filter(isMeaningfulEvent).slice(0, 12);
  els.eventSummary.textContent = events.length ? `${events.length} decisions or exceptions` : "No decisions or exceptions";
  if (events.length === 0) {
    els.eventRows.innerHTML = '<tr><td colspan="4" class="empty-cell">No recent events</td></tr>';
    return;
  }
  els.eventRows.replaceChildren(...events.map(eventRow));
}

function isMeaningfulEvent(event) {
  if (!event || event.kind === "node_heartbeat") return false;
  if (severityForEvent(event) !== "INFO") return true;
  if (event.kind !== "autoscaler_cycle") return true;
  const data = event.data || {};
  return actionSummary([...(data.actions || []), ...(data.builder_actions || [])]) !== "Hold"
    || resourceHasPositiveValue(data.resource_deficit);
}

async function refreshSandboxes(options = {}) {
  if (state.sandboxFetchInFlight) return;
  state.sandboxFetchInFlight = true;
  renderSandboxesPage();
  if (!options.quiet) {
    setSandboxPageStatus("Loading", "warn");
  }
  try {
    const payload = await dashboardJsonRequest("/v1/sandboxes?refresh=true");
    const rows = Array.isArray(payload.sandboxes) ? payload.sandboxes : [];
    state.lastSandboxes = rows.map(normalizeSandboxRecord).sort(compareSandboxesNewestFirst);
    state.lastSandboxRefreshAt = Date.now();
    setSandboxPageStatus(payload.cached ? "Cached" : "Live", "ok");
    setText(
      "sandboxesPageDetail",
      state.lastSandboxes.length
        ? `${formatInteger(state.lastSandboxes.length)} latest sandbox records loaded`
        : "No sandboxes are currently listed"
    );
    renderSandboxesPage();
  } catch (error) {
    const message = errorMessage(error);
    setSandboxPageStatus("Failed", "bad");
    setText("sandboxesPageDetail", message);
    setText("sandboxesPageSummary", message);
  } finally {
    state.sandboxFetchInFlight = false;
    renderSandboxesPage();
  }
}

function setSandboxPageStatus(text, mode) {
  if (!els.sandboxesPageStatusBadge) return;
  els.sandboxesPageStatusBadge.textContent = text;
  els.sandboxesPageStatusBadge.className = `inline-badge ${mode === "ok" ? "badge-ok" : mode === "bad" ? "badge-bad" : "badge-warn"}`;
}

function renderSandboxesPage() {
  if (!els.sandboxesPage) return;
  const snapshot = state.lastSnapshot || {};
  const metrics = snapshot.sandboxes || {};
  const query = String(els.sandboxSearchInput.value || "").trim().toLowerCase();
  const stateFilter = String(els.sandboxStateFilter.value || "all");
  const sandboxes = state.lastSandboxes || [];
  const filtered = sandboxes.filter((sandbox) => sandboxMatchesSearch(sandbox, query) && sandboxMatchesState(sandbox, stateFilter));
  const activeRoutes = asNumber(metrics.active_routes);
  const staleRoutes = asNumber(metrics.stale_routes);
  const stateCounts = sandboxes.reduce((counts, sandbox) => {
    const group = sandboxStateGroup(sandbox.state);
    counts[group] = (counts[group] || 0) + 1;
    return counts;
  }, {});

  setText("sandboxesPageRowsValue", formatInteger(sandboxes.length));
  setText("sandboxesPageTerminableValue", formatInteger(stateCounts.running));
  setText("sandboxesPagePendingValue", formatInteger(stateCounts.parked));
  setText("sandboxesPageRoutesValue", formatInteger((stateCounts.transitioning || 0) + (stateCounts.migrating || 0)));
  setText(
    "sandboxesPageSummary",
    `${formatInteger(filtered.length)} shown of ${formatInteger(sandboxes.length)} loaded, ${formatInteger(activeRoutes)} active routes${staleRoutes ? `, ${formatInteger(staleRoutes)} stale` : ""}`
  );
  setText(
    "sandboxListSummary",
    filtered.length
      ? `${formatInteger(filtered.length)} latest`
      : (sandboxes.length ? "No matching sandboxes" : "No sandboxes loaded")
  );

  els.refreshSandboxesButton.disabled = state.sandboxFetchInFlight;
  els.refreshSandboxesButton.textContent = state.sandboxFetchInFlight ? "Refreshing" : "Refresh";

  if (filtered.length === 0) {
    renderEmptyRow(
      els.sandboxRows,
      8,
      sandboxes.length ? "No sandboxes match the current search" : "No sandboxes loaded"
    );
    return;
  }
  els.sandboxRows.replaceChildren(...filtered.slice(0, 250).map(sandboxRow));
}

function normalizeSandboxRecord(record) {
  const raw = plainObject(record);
  const spec = plainObject(raw.spec);
  const node = plainObject(raw.node);
  return {
    ...raw,
    profile: spec.profile,
    node: node.node_id,
    nodeFresh: node.fresh,
    resources: plainObject(raw.resources),
    spec,
    labels: plainObject(raw.labels),
  };
}

function compareSandboxesNewestFirst(a, b) {
  const aTime = Date.parse(a.created_at || a.updated_at || "") || 0;
  const bTime = Date.parse(b.created_at || b.updated_at || "") || 0;
  if (aTime !== bTime) return bTime - aTime;
  return String(a.id || "").localeCompare(String(b.id || ""));
}

function sandboxMatchesSearch(sandbox, query) {
  if (!query) return true;
  return [
    sandbox.id,
    sandbox.state,
    sandbox.image,
    sandbox.profile,
    sandbox.node,
    labelsText(sandbox.labels),
  ].join(" ").toLowerCase().includes(query);
}

function sandboxMatchesState(sandbox, filter) {
  if (!filter || filter === "all") return true;
  const group = sandboxStateGroup(sandbox.state);
  if (filter === "attention") return ["failed", "transitioning", "pending"].includes(group) || sandbox.nodeFresh === false;
  if (filter === "failed") return group === "failed" || sandbox.nodeFresh === false;
  return group === filter;
}

function sandboxStateGroup(state) {
  const status = String(state || "unknown").toLowerCase();
  if (status === "running") return "running";
  if (status === "parked") return "parked";
  if (["hibernating", "restoring", "waking", "import_planned", "importing", "import_ready", "moving_out"].includes(status)) return "transitioning";
  if (["creating", "planned", "quota_ready", "rootfs_ready"].includes(status)) return "pending";
  if (["recovery-required", "unavailable"].includes(status)) return "failed";
  return "unknown";
}

function canTerminateSandbox(sandbox) {
  return Boolean(sandbox && sandbox.id);
}

function sandboxRow(sandbox) {
  const tr = document.createElement("tr");
  const statusCell = document.createElement("td");
  const badge = document.createElement("span");
  const status = String(sandbox.state || "unknown").toLowerCase();
  badge.className = `sandbox-status ${sandboxStatusClass(status)}`;
  badge.textContent = status || "unknown";
  statusCell.append(badge);
  tr.append(statusCell);

  appendClassCell(
    tr,
    sandbox.generation === null ? (sandbox.id || "-") : `${sandbox.id || "-"} / g${formatInteger(sandbox.generation)}`,
    "sandbox-id",
    [sandbox.id, sandbox.operation_id].filter(Boolean).join("\n")
  );
  appendClassCell(tr, sandbox.image || "-", "sandbox-image", sandbox.image || "");
  appendClassCell(tr, sandboxNodeText(sandbox), "sandbox-node", sandboxNodeTitle(sandbox));
  appendClassCell(tr, sandboxResourcesText(sandbox), "sandbox-resources", sandboxResourcesText(sandbox));
  appendCell(tr, sandboxAge(sandbox));
  appendClassCell(tr, labelsText(sandbox.labels), "sandbox-labels", labelsTitle(sandbox.labels));

  const actionCell = document.createElement("td");
  const button = document.createElement("button");
  const terminating = sandbox.id && state.terminatingSandboxIds.has(sandbox.id);
  button.className = "table-action danger";
  button.type = "button";
  button.textContent = terminating ? "Terminating" : "Terminate";
  button.disabled = !sandbox.id || state.sandboxActionInFlight || Boolean(terminating);
  button.title = sandbox.id ? `Terminate ${sandbox.id}` : "No sandbox id available";
  button.addEventListener("click", () => terminateSandbox(sandbox.id));
  actionCell.append(button);
  tr.append(actionCell);
  return tr;
}

function sandboxStatusClass(status) {
  const group = sandboxStateGroup(status);
  if (["running", "parked", "transitioning", "pending", "failed"].includes(group)) return group;
  return "unknown";
}

function sandboxNodeText(sandbox) {
  const suffix = sandbox.nodeFresh === false ? " stale" : "";
  return `${sandbox.node || "-"}${suffix}`;
}

function sandboxNodeTitle(sandbox) {
  const parts = [];
  if (sandbox.node) parts.push(sandbox.node);
  if (sandbox.cached) parts.push("cached route");
  if (sandbox.routeOnly) parts.push("route only");
  if (sandbox.nodeFresh === false) parts.push("stale heartbeat");
  return parts.join(", ");
}

function sandboxResourcesText(sandbox) {
  const resources = sandbox.resources || {};
  const spec = sandbox.spec || {};
  const cpu = firstNumber(resources.vcpu, spec.cpus);
  const memory = firstNumber(resources.memory_mb, spec.memory_mb);
  const disk = firstNumber(resources.disk_mb, spec.disk_mb);
  const parts = [];
  if (cpu !== null && cpu > 0) parts.push(`${formatNumber(cpu)} vCPU`);
  if (memory !== null && memory > 0) parts.push(formatMemory(memory));
  if (disk !== null && disk > 0) parts.push(formatMemory(disk));
  return parts.join(" / ") || "-";
}

function sandboxAge(sandbox) {
  const created = Date.parse(sandbox.created_at || "");
  if (!Number.isFinite(created)) return "-";
  return formatAge((Date.now() - created) / 1000);
}

function labelsText(labels) {
  const entries = Object.entries(plainObject(labels)).sort(([a], [b]) => a.localeCompare(b));
  if (!entries.length) return "-";
  return entries.slice(0, 4).map(([key, value]) => `${key}=${value}`).join(", ");
}

function labelsTitle(labels) {
  const entries = Object.entries(plainObject(labels)).sort(([a], [b]) => a.localeCompare(b));
  return entries.map(([key, value]) => `${key}=${value}`).join("\n");
}

async function terminateSandbox(sandboxId) {
  const id = String(sandboxId || "").trim();
  if (!id) return;
  if (!window.confirm(`Terminate sandbox ${id}?`)) return;
  state.terminatingSandboxIds.add(id);
  renderSandboxesPage();
  try {
    await deleteSandboxById(id);
    setSandboxPageStatus("Terminated", "ok");
    setText("sandboxesPageDetail", `${id} terminated`);
  } catch (error) {
    setSandboxPageStatus("Failed", "bad");
    setText("sandboxesPageDetail", errorMessage(error));
  } finally {
    state.terminatingSandboxIds.delete(id);
    await refreshSandboxes({ force: true, quiet: true });
  }
}

async function terminateAllSandboxes() {
  const candidates = (state.lastSandboxes || []).filter(canTerminateSandbox);
  if (!candidates.length) return;
  if (!window.confirm(`Terminate all ${candidates.length} listed sandboxes?`)) return;
  state.sandboxActionInFlight = true;
  renderSandboxesPage();
  const failures = [];
  for (const [index, sandbox] of candidates.entries()) {
    state.terminatingSandboxIds.add(sandbox.id);
    setText(
      "sandboxesPageDetail",
      `Terminating ${formatInteger(index + 1)} of ${formatInteger(candidates.length)}: ${sandbox.id}`
    );
    try {
      await deleteSandboxById(sandbox.id);
    } catch (error) {
      failures.push(`${sandbox.id}: ${errorMessage(error)}`);
    } finally {
      state.terminatingSandboxIds.delete(sandbox.id);
    }
  }
  state.sandboxActionInFlight = false;
  await refreshSandboxes({ force: true, quiet: true });
  if (failures.length) {
    setSandboxPageStatus("Partial", "bad");
    setText("sandboxesPageDetail", `${failures.length} terminate request(s) failed: ${failures.slice(0, 3).join("; ")}`);
  } else {
    setSandboxPageStatus("Terminated", "ok");
    setText("sandboxesPageDetail", `${candidates.length} sandbox terminate request(s) completed`);
  }
}

async function deleteSandboxById(sandboxId) {
  return dashboardJsonRequest(`/v1/sandboxes/${encodeURIComponent(sandboxId)}`, {
    method: "DELETE",
  });
}

function renderRegistryPage(snapshot) {
  if (!els.registryPage) return;
  const registry = snapshot.registry || {};
  const images = snapshot.images || {};
  const builds = Array.isArray(images.builds) ? images.builds.slice() : [];
  const registryBuilds = pushedRegistryBuilds(builds);
  const buildByTag = buildsByRegistryTag(registryBuilds);
  const configured = Boolean(registry.configured);
  const ok = Boolean(registry.ok);
  const repos = Array.isArray(registry.repositories) ? registry.repositories : [];
  const query = String(els.registrySearchInput.value || "").trim().toLowerCase();
  const filter = String(els.registryFilterSelect.value || "all");

  setText("registryActiveBuildsValue", formatInteger(images.active_builds));
  setText("registryFailedBuildsValue", formatInteger(images.failed_builds));
  setText("registryPendingBuildsValue", formatInteger(images.pending_builds));
  setText("registryOldestBuildValue", asNumber(images.pending_builds) ? formatAge(images.oldest_pending_build_seconds) : "-");
  setText("registryActiveBuildsSummaryValue", formatInteger(images.active_builds));
  setText("registryFailedBuildsSummaryValue", formatInteger(images.failed_builds));
  drawBuilderChart();

  els.registryPageStatusBadge.textContent = configured ? (ok ? "Online" : "Offline") : "Not set";
  els.registryPageStatusBadge.className = `inline-badge ${configured ? (ok ? "badge-ok" : "badge-bad") : "badge-muted"}`;
  setText("registryPageUrl", registry.url || "No registry configured");
  setText("registryPageReposValue", configured ? formatInteger(registry.repository_count) : "-");
  setText("registryPageTagsValue", configured ? formatInteger(registry.scanned_tag_count) : "-");
  setText("registryPageVisibleTagsValue", configured ? formatInteger(registry.visible_tag_count) : "-");
  const scanned = asNumber(registry.scanned_repository_count);
  const total = asNumber(registry.repository_count);
  setText("registryPageCoverageValue", configured && total > 0 ? `${formatInteger(scanned)}/${formatInteger(total)}` : "-");
  const disk = registry.disk && typeof registry.disk === "object" ? registry.disk : null;
  const diskPercent = disk ? nullableNumber(disk.used_percent) : null;
  setText("registryPageDiskValue", diskPercent === null ? "-" : `${formatNumber(diskPercent)}%`);
  const diskDetail = disk && disk.state && disk.state !== "ok" && diskPercent !== null
    ? `; disk ${formatNumber(diskPercent)}% full, ${disk.state === "refusing" ? `image builds refused above ${formatNumber(disk.refuse_percent)}%` : `cleanup above ${formatNumber(disk.cleanup_percent)}%`}`
    : "";

  if (!configured) {
    setText("registryPageHealthDetail", "Configure the deployment registry to show registry health.");
    setText("registryPageSummary", "No registry configured");
    setText("registryRepoSummary", "No repositories loaded");
    setText("registryTagSummary", "No tags loaded");
    renderEmptyRow(els.registryRepoRows, 5, "No registry configured");
    renderEmptyRow(els.registryTagRows, 5, "No registry configured");
    renderRegistryBuildRows(registryBuilds);
    return;
  }
  if (!ok) {
    setText("registryPageHealthDetail", registry.error ? `Registry check failed: ${registry.error}` : "Registry check failed");
    setText("registryPageSummary", "Registry unavailable");
    setText("registryRepoSummary", "No repositories loaded");
    setText("registryTagSummary", "No tags loaded");
    renderEmptyRow(els.registryRepoRows, 5, "Registry unavailable");
    renderEmptyRow(els.registryTagRows, 5, "Registry unavailable");
    renderRegistryBuildRows(registryBuilds);
    return;
  }

  const truncated = registry.catalog_truncated ? ", catalog truncated" : "";
  const unavailable = asNumber(registry.unavailable_repository_count);
  const partial = unavailable > 0 ? `, ${formatInteger(unavailable)} missing tag lists` : "";
  setText(
    "registryPageHealthDetail",
    `${formatInteger(scanned)} repositories scanned, ${formatInteger(registry.scanned_tag_count)} tags observed${truncated}${partial}${diskDetail}`
  );

  const filteredRepos = repos.filter((repo) => registryRepoMatches(repo, buildByTag, filter, query));
  const shownRepos = filteredRepos.slice(0, MAX_REGISTRY_REPOSITORY_ROWS);
  const flattenedTags = flattenRegistryTags(filteredRepos, buildByTag)
    .filter((item) => !query || matchesRegistrySearch(item.searchText, query));
  const summaryParts = [
    `${formatInteger(filteredRepos.length)} repositories`,
    `${formatInteger(flattenedTags.length)} visible tags`,
    `${formatInteger(registryBuilds.length)} pushed builds`,
  ];
  if (unavailable > 0) summaryParts.push(`${formatInteger(unavailable)} missing tag lists`);
  if (query) summaryParts.push(`matching "${query}"`);
  setText("registryPageSummary", summaryParts.join(", "));
  setText(
    "registryRepoSummary",
    `${formatInteger(shownRepos.length)} shown of ${formatInteger(filteredRepos.length)} matching`
  );
  setText(
    "registryTagSummary",
    `${formatInteger(Math.min(flattenedTags.length, 200))} shown of ${formatInteger(flattenedTags.length)} matching`
  );

  if (shownRepos.length === 0) {
    renderEmptyRow(els.registryRepoRows, 5, "No repositories match the current filter");
  } else {
    els.registryRepoRows.replaceChildren(...shownRepos.map((repo) => registryRepoRow(repo, buildByTag)));
  }
  if (flattenedTags.length === 0) {
    renderEmptyRow(els.registryTagRows, 5, "No tags match the current filter");
  } else {
    els.registryTagRows.replaceChildren(...flattenedTags.slice(0, 200).map(registryTagRow));
  }
  renderRegistryBuildRows(registryBuilds.filter((build) => !query || matchesRegistrySearch(buildSearchText(build), query)));
}

function registryRepoMatches(repo, buildByTag, filter, query) {
  const tags = Array.isArray(repo.tags) ? repo.tags : [];
  const builds = tags.flatMap((tag) => buildByTag.get(`${repo.repository}:${tag}`) || []);
  if (filter === "with-builds" && builds.length === 0) return false;
  if (filter === "truncated" && !repo.tags_truncated) return false;
  if (filter === "empty" && tags.length > 0) return false;
  if (!query) return true;
  return matchesRegistrySearch([
    repo.repository,
    repo.namespace,
    repo.latest_tag,
    tags.join(" "),
    builds.map(buildSearchText).join(" "),
  ].join(" "), query);
}

function flattenRegistryTags(repos, buildByTag) {
  const items = [];
  for (const repo of repos) {
    const tags = Array.isArray(repo.tags) ? repo.tags : [];
    for (const tag of tags) {
      const key = `${repo.repository}:${tag}`;
      const builds = buildByTag.get(key) || [];
      items.push({
        repository: repo.repository || "-",
        tag,
        key,
        builds,
        latestBuild: latestBuild(builds),
        searchText: [
          repo.repository,
          tag,
          ...builds.map(buildSearchText),
        ].join(" "),
      });
    }
  }
  items.sort((a, b) => a.repository.localeCompare(b.repository) || b.tag.localeCompare(a.tag));
  return items;
}

function registryRepoRow(repo, buildByTag) {
  const tr = document.createElement("tr");
  const tags = Array.isArray(repo.tags) ? repo.tags : [];
  const buildCount = tags.reduce((total, tag) => total + (buildByTag.get(`${repo.repository}:${tag}`)?.length || 0), 0);
  appendCell(tr, repo.repository || "-");
  appendCell(tr, formatInteger(repo.tag_count));
  appendCell(tr, repo.available === false ? "Unavailable" : (repo.latest_tag || "-"));
  appendCell(tr, formatInteger(buildCount));
  const tagCell = document.createElement("td");
  if (repo.available === false) {
    tagCell.textContent = repo.error || "Tags unavailable";
  } else {
    appendTagChips(tagCell, tags, repo.tags_truncated, repo.tag_count);
  }
  tr.append(tagCell);
  return tr;
}

function registryTagRow(item) {
  const tr = document.createElement("tr");
  const build = item.latestBuild || {};
  appendCell(tr, item.repository);
  appendCell(tr, item.tag || "-");
  appendCell(tr, build.status || "-");
  appendCell(tr, build.image_id || "-");
  appendCell(tr, buildLocation(build));
  return tr;
}

function renderRegistryBuildRows(builds) {
  const ordered = builds.slice().sort((a, b) => (Date.parse(b.updated_at) || 0) - (Date.parse(a.updated_at) || 0));
  setText("registryBuildSummary", ordered.length ? `${formatInteger(ordered.length)} pushed builds` : "No pushed builds loaded");
  if (ordered.length === 0) {
    renderEmptyRow(els.registryBuildRows, 6, "No pushed builds loaded");
    return;
  }
  els.registryBuildRows.replaceChildren(...ordered.slice(0, 100).map(buildRow));
}

function pushedRegistryBuilds(builds) {
  return builds.filter((build) => {
    const image = build.image || {};
    return Boolean(build.push || image.pushed || imageTagHasRegistryHost(build.tag));
  });
}

function buildsByRegistryTag(builds) {
  const byTag = new Map();
  for (const build of builds) {
    const tag = String(build.tag || "");
    const repository = registryRepositoryFromTag(tag);
    const tagName = registryTagName(tag);
    if (!repository || !tagName) continue;
    const key = `${repository}:${tagName}`;
    const items = byTag.get(key) || [];
    items.push(build);
    byTag.set(key, items);
  }
  return byTag;
}

function registryRepositoryFromTag(imageTag) {
  const parts = splitRegistryTag(imageTag);
  return parts.repository;
}

function registryTagName(imageTag) {
  const parts = splitRegistryTag(imageTag);
  return parts.tag;
}

function splitRegistryTag(imageTag) {
  const raw = String(imageTag || "").trim();
  if (!raw) return { repository: "", tag: "" };
  const lastSlash = raw.lastIndexOf("/");
  const lastColon = raw.lastIndexOf(":");
  const hasTag = lastColon > lastSlash;
  const name = hasTag ? raw.slice(0, lastColon) : raw;
  const tag = hasTag ? raw.slice(lastColon + 1) : "";
  const segments = name.split("/").filter(Boolean);
  if (segments.length > 1 && isRegistryHostSegment(segments[0])) {
    segments.shift();
  }
  return { repository: segments.join("/"), tag };
}

function isRegistryHostSegment(segment) {
  return segment === "localhost" || segment.includes(".") || segment.includes(":");
}

function imageTagHasRegistryHost(imageTag) {
  const raw = String(imageTag || "").trim();
  const firstSlash = raw.indexOf("/");
  if (firstSlash < 0) return false;
  return isRegistryHostSegment(raw.slice(0, firstSlash));
}

function latestBuild(builds) {
  if (!Array.isArray(builds) || builds.length === 0) return null;
  return builds.slice().sort((a, b) => (Date.parse(b.updated_at) || 0) - (Date.parse(a.updated_at) || 0))[0];
}

function appendTagChips(cell, tags, truncated, totalTagCount) {
  if (!tags.length) {
    cell.textContent = "-";
    return;
  }
  const list = document.createElement("div");
  list.className = "tag-chip-list";
  for (const tag of tags.slice(0, 10)) {
    const chip = document.createElement("span");
    chip.className = "tag-chip";
    chip.textContent = tag;
    chip.title = tag;
    list.append(chip);
  }
  if (truncated || tags.length > 10) {
    const more = document.createElement("span");
    more.className = "tag-chip";
    const hidden = Math.max(0, asNumber(totalTagCount) - Math.min(tags.length, 10));
    more.textContent = hidden > 0 ? `+${hidden} older` : "older tags omitted";
    more.title = "Older tags omitted from the dashboard payload";
    list.append(more);
  }
  cell.append(list);
}

function buildSearchText(build) {
  return [
    build.image_id,
    build.tag,
    build.status,
    buildLocation(build),
    buildDetails(build),
  ].join(" ");
}

function matchesRegistrySearch(text, query) {
  if (!query) return true;
  return String(text || "").toLowerCase().includes(query);
}

function renderEmptyRow(tbody, columns, message) {
  const tr = document.createElement("tr");
  const td = document.createElement("td");
  td.colSpan = columns;
  td.className = "empty-cell";
  td.textContent = message;
  tr.append(td);
  tbody.replaceChildren(tr);
}

function repoPill(repo) {
  const item = document.createElement("span");
  item.className = "repo-pill";
  const tags = Array.isArray(repo.tags) ? repo.tags : [];
  const latest = tags.length ? `:${tags[tags.length - 1]}` : "";
  const unavailable = repo.available === false;
  item.textContent = unavailable
    ? `${repo.repository || "repository"} (tags unavailable)`
    : `${repo.repository || "repository"}${latest} (${asNumber(repo.tag_count)})`;
  item.title = unavailable ? (repo.error || "Tags unavailable") : tags.join(", ");
  return item;
}

function renderBuilds(snapshot) {
  const images = snapshot.images || {};
  const builds = Array.isArray(images.builds) ? images.builds.slice() : [];
  builds.sort((a, b) => (Date.parse(b.updated_at) || 0) - (Date.parse(a.updated_at) || 0));
  const active = builds.filter((build) => !["succeeded", "failed"].includes(String(build.status || ""))).length;
  els.buildSummary.textContent = builds.length
    ? `${builds.length} tracked, ${active} active`
    : "No tracked builds";
  if (builds.length === 0) {
    els.buildRows.innerHTML = '<tr><td colspan="6" class="empty-cell">No image builds tracked yet</td></tr>';
    return;
  }
  els.buildRows.replaceChildren(...builds.slice(0, 10).map(buildRow));
}

function buildRow(build) {
  const tr = document.createElement("tr");
  const statusCell = document.createElement("td");
  const status = String(build.status || "unknown");
  const badge = document.createElement("span");
  badge.className = `build-status ${statusClass(status)}`;
  badge.textContent = status;
  statusCell.append(badge);
  tr.append(statusCell);
  appendCell(tr, build.image_id || "-");
  appendCell(tr, build.tag || "-");
  appendCell(tr, buildLocation(build));
  appendCell(tr, buildAge(build));
  appendCell(tr, buildDetails(build));
  return tr;
}

function statusClass(status) {
  if (status === "succeeded" || status === "failed" || status === "running" || status === "queued") {
    return status;
  }
  return "unknown";
}

function buildLocation(build) {
  if (build.location) return build.location;
  const node = build.node || {};
  return node.node_id || node.job_id || "-";
}

function buildAge(build) {
  const started = Date.parse(build.started_at || build.created_at);
  const finished = Date.parse(build.finished_at || build.updated_at);
  if (!Number.isFinite(started)) return "-";
  if (String(build.status || "") === "running") {
    return `${formatAge((Date.now() - started) / 1000)} running`;
  }
  if (!Number.isFinite(finished) || finished < started) {
    return formatAge((Date.now() - started) / 1000);
  }
  return formatAge((finished - started) / 1000);
}

function buildDetails(build) {
  if (build.error) return build.error;
  const parts = [];
  const timings = build.timings || {};
  const phases = timings.phases || {};
  if (Number.isFinite(Number(timings.total_ms))) parts.push(`total ${formatDurationMs(timings.total_ms)}`);
  if (Number.isFinite(Number(phases.docker_build_ms))) parts.push(`build ${formatDurationMs(phases.docker_build_ms)}`);
  if (Number.isFinite(Number(phases.docker_push_ms))) parts.push(`push ${formatDurationMs(phases.docker_push_ms)}`);
  if (build.push) parts.push("push enabled");
  if (build.exit_code !== null && build.exit_code !== undefined) parts.push(`build exit ${build.exit_code}`);
  if (build.push_exit_code !== null && build.push_exit_code !== undefined) parts.push(`push exit ${build.push_exit_code}`);
  const tail = String(build.log_tail || "").trim().split("\n").filter(Boolean).slice(-1)[0];
  if (tail) parts.push(tail.slice(0, 160));
  return parts.join(", ") || "-";
}

function renderTelemetry(snapshot) {
  const telemetry = snapshot.telemetry || {};
  els.telemetrySummary.textContent = telemetry.enabled
    ? `${formatInteger(telemetry.exported_spans)} spans exported`
    : "Disabled";
  const tr = document.createElement("tr");
  appendCell(tr, telemetry.enabled ? "Enabled" : "Disabled");
  appendCell(tr, `${formatInteger(telemetry.queue_size)} / ${formatInteger(telemetry.queue_capacity)}`);
  appendCell(tr, formatInteger(telemetry.accepted_spans));
  appendCell(tr, formatInteger(telemetry.exported_spans));
  appendCell(tr, `${formatInteger(telemetry.dropped_spans)} / ${formatInteger(telemetry.failed_exports)}`);
  els.telemetryRows.replaceChildren(tr);
}

function eventRow(event) {
  const tr = document.createElement("tr");
  const severity = severityForEvent(event);
  appendCell(tr, formatTime(event.timestamp));
  const severityCell = document.createElement("td");
  const badge = document.createElement("span");
  badge.className = `severity-badge severity-${severity.toLowerCase()}`;
  badge.textContent = severity;
  severityCell.append(badge);
  tr.append(severityCell);
  appendCell(tr, titleForEvent(event));
  appendCell(tr, summarizeEvent(event));
  return tr;
}

function appendCell(row, value) {
  const td = document.createElement("td");
  td.textContent = value;
  row.append(td);
}

function appendClassCell(row, value, className, title = "") {
  const td = document.createElement("td");
  td.textContent = value;
  if (className) td.className = className;
  if (title) td.title = title;
  row.append(td);
}

function severityForEvent(event) {
  const data = event.data || {};
  if (event.kind === "sandbox_pending_deleted") return "ALERT";
  if (event.kind === "autoscaler_cycle") {
    const actions = Array.isArray(data.actions) ? data.actions : [];
    const builderActions = Array.isArray(data.builder_actions) ? data.builder_actions : [];
    const hasDeficit = resourceHasPositiveValue(data.resource_deficit);
    if (hasDeficit) return "WARN";
    if (actions.some((action) => ["create", "scale_up"].includes(actionKind(action)))
      || builderActions.some((action) => actionKind(action) === "scale_up_builder")) return "INFO";
    return "INFO";
  }
  if (event.kind === "node_heartbeat") {
    const actual = data.actual_usage || {};
    const load = data.load || {};
    if (asNumber(actual.memory_percent) >= 85 || asNumber(load.vcpu) >= 0.9) return "WARN";
    return "INFO";
  }
  return "INFO";
}

function titleForEvent(event) {
  if (event.kind === "autoscaler_cycle") return "Autoscaler cycle";
  if (event.kind === "sandbox_scheduled") return "Sandbox scheduled";
  if (event.kind === "sandbox_pending_deleted") return "Sandbox pending deleted";
  if (event.kind === "node_heartbeat") return "Node heartbeat";
  return event.kind || "Event";
}

function summarizeEvent(event) {
  const data = event.data || {};
  if (event.kind === "autoscaler_cycle") {
    const actions = Array.isArray(data.actions) ? data.actions : [];
    const builderActions = Array.isArray(data.builder_actions) ? data.builder_actions : [];
    const created = Array.isArray(data.created_job_ids) ? data.created_job_ids.length : 0;
    const stopped = Array.isArray(data.stop_job_ids) ? data.stop_job_ids.length : 0;
    const scaleDown = ((data.execution || {}).scale_down || {});
    const stopRequested = Array.isArray(scaleDown.requested_job_ids) ? scaleDown.requested_job_ids.length : 0;
    const stopBlocked = Array.isArray(scaleDown.blocked_job_ids) ? scaleDown.blocked_job_ids.length : 0;
    const stopDraining = Array.isArray(scaleDown.draining_job_ids) ? scaleDown.draining_job_ids.length : 0;
    const stopTerminated = Array.isArray(scaleDown.terminated_job_ids) ? scaleDown.terminated_job_ids.length : stopped;
    const pending = data.pending_resources || {};
    const prepared = data.prepared_resources || {};
    const actionText = actionSummary(actions.concat(builderActions));
    const reasons = Array.isArray(data.reasons) && data.reasons.length ? `, because ${data.reasons.slice(0, 2).join("; ")}` : "";
    return `ready ${asNumber(data.ready_nodes)}, provisioning ${asNumber(data.provisioning_nodes)}, created ${created}, stops requested ${stopRequested}, draining ${stopDraining}, blocked ${stopBlocked}, terminated ${stopTerminated}, pending ${formatResources(pending)}, prepared ${formatResources(prepared)}, decision ${actionText}${reasons}`;
  }
  if (event.kind === "sandbox_scheduled") {
    return `${data.sandbox_id || "sandbox"} on ${data.node_id || data.job_id || "node"}, wait ${formatDurationMs(data.scale_up_wait_ms)}`;
  }
  if (event.kind === "sandbox_pending_deleted") {
    return `${data.sandbox_id || "sandbox"} deleted while pending after ${formatDurationMs(data.pending_age_ms)}`;
  }
  if (event.kind === "node_heartbeat") {
    const actual = data.actual_usage || {};
    const load = data.load || {};
    return `${data.node_id || data.job_id || "node"} active ${asNumber(data.active_sandboxes)}, CPU ${formatPercentPoint(ratioToPercent(load.vcpu))} reserved, actual ${formatPercentPoint(actual.cpu_percent)}`;
  }
  return JSON.stringify(data).slice(0, 240);
}

function redrawCharts() {
  if (document.hidden) return;
  if (state.currentPage === "registry") {
    drawBuilderChart();
    return;
  }
  if (state.currentPage !== "overview") return;
  if (state.history.length === 0) {
    for (const id of [
      "activeNodesChart",
      "activeSandboxesChart",
      "queueDepthChart",
      "cpuPressureChart",
      "memoryPressureChart",
      "scaleLatencyChart",
      "sandboxStartChart",
      "builderBuildsChart",
      "nodesSpark",
      "sandboxesSpark",
      "cpuSpark",
      "memorySpark",
      "queueSpark",
      "errorSpark",
    ]) {
      clearPlot(id, "Waiting for metrics");
    }
    return;
  }

  drawLineChart("activeNodesChart", [
    { label: "Ready nodes", color: palette.blue, values: state.history.map((p) => p.activeNodes) },
    { label: "Sandbox routes", color: palette.green, fill: palette.greenSoft, values: state.history.map((p) => p.activeSandboxes) },
  ], { min: 0, ticks: 4 });
  drawLineChart("queueDepthChart", [
    { label: "Ready to wake", color: palette.green, fill: palette.greenSoft, values: state.history.map((p) => p.readyWake) },
    { label: "Sandbox creates", color: palette.purple, values: state.history.map((p) => p.pendingSandboxes) },
    { label: "Image builds", color: palette.orange, dashed: true, values: state.history.map((p) => p.pendingBuilds) },
  ], { min: 0, ticks: 4, integerAxis: true });
  drawLineChart("cpuPressureChart", [
    { label: "Actual", color: palette.green, fill: palette.greenSoft, values: state.history.map((p) => p.cpuUtilization) },
    { label: "Reserved", color: palette.blue, dashed: true, values: state.history.map((p) => p.cpuReserved) },
  ], { min: 0, max: 100, ticks: 4, suffix: "%" });
  drawLineChart("memoryPressureChart", [
    { label: "Actual", color: palette.orange, fill: palette.orangeSoft, values: state.history.map((p) => p.memoryUtilization) },
    { label: "Reserved", color: palette.blue, dashed: true, values: state.history.map((p) => p.memoryReserved) },
  ], { min: 0, max: 100, ticks: 4, suffix: "%" });
  drawLineChart("sandboxStartChart", [
    { label: "Model wait p95", color: palette.purple, values: state.history.map((p) => p.modelWaitP95Seconds) },
    { label: "Wake p95", color: palette.green, fill: palette.greenSoft, values: state.history.map((p) => p.wakeP95Seconds) },
  ], { min: 0, ticks: 4 });
}

function drawBuilderChart() {
  if (state.history.length === 0) {
    clearPlot("builderBuildsChart", "Waiting for metrics");
    return;
  }
  drawLineChart("builderBuildsChart", [
    { label: "Active builds", color: palette.orange, fill: palette.orangeSoft, values: state.history.map((p) => p.activeBuilds) },
    { label: "Ready builders", color: palette.blue, dashed: true, values: state.history.map((p) => p.builderNodes) },
  ], { min: 0, ticks: 4, integerAxis: true });
}

function scheduleChartRedraw() {
  if (state.resizeFrame !== null) return;
  state.resizeFrame = window.requestAnimationFrame(() => {
    state.resizeFrame = null;
    redrawCharts();
  });
}

function clearPlot(id, label) {
  const canvas = document.getElementById(id);
  if (!canvas) return;
  const prepared = prepareCanvas(canvas);
  const ctx = prepared.ctx;
  ctx.fillStyle = palette.plotBg;
  ctx.fillRect(0, 0, prepared.width, prepared.height);
  ctx.fillStyle = palette.muted;
  ctx.font = "13px system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(label, prepared.width / 2, prepared.height / 2);
}

function drawSpark(id, values, color, fill, options = {}) {
  const canvas = document.getElementById(id);
  if (!canvas) return;
  const prepared = prepareCanvas(canvas);
  const ctx = prepared.ctx;
  const width = prepared.width;
  const height = prepared.height;
  ctx.clearRect(0, 0, width, height);
  const numeric = values.filter((value) => value !== null && Number.isFinite(value));
  if (numeric.length === 0) return;
  const min = options.min ?? Math.min(...numeric, 0);
  const rawMax = options.max ?? Math.max(...numeric);
  const max = rawMax <= min ? min + 1 : rawMax;
  const points = valuesToPoints(values, width, height, { left: 0, right: 0, top: 6, bottom: 4 }, min, max);
  fillUnderLine(ctx, points, height - 4, fill);
  strokeLine(ctx, points, color, false, 2);
}

function drawLineChart(id, series, options = {}) {
  const canvas = document.getElementById(id);
  if (!canvas) return;
  const prepared = prepareCanvas(canvas);
  const ctx = prepared.ctx;
  const width = prepared.width;
  const height = prepared.height;
  const pad = { left: 42, right: 14, top: 10, bottom: 28 };
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = palette.plotBg;
  ctx.fillRect(0, 0, width, height);

  const allValues = series.flatMap((line) => line.values).filter((value) => value !== null && Number.isFinite(value));
  if (allValues.length === 0) {
    drawEmptyPlot(ctx, width, height, "No numeric samples");
    return;
  }

  const min = options.min ?? Math.min(...allValues);
  const rawMax = options.max ?? Math.max(...allValues);
  const max = rawMax <= min ? min + 1 : rawMax * 1.08;
  drawGrid(ctx, width, height, pad, min, max, options);

  for (const line of series) {
    const points = valuesToPoints(line.values, width, height, pad, min, max);
    if (line.fill) {
      fillUnderLine(ctx, points, height - pad.bottom, line.fill);
    }
    strokeLine(ctx, points, line.color, Boolean(line.dashed), 2);
  }
  const summaries = series.map((line) => {
    const numeric = line.values.filter((value) => value !== null && Number.isFinite(value));
    const latest = [...line.values].reverse().find((value) => value !== null && Number.isFinite(value));
    if (!numeric.length) return `${line.label}: unavailable`;
    return `${line.label}: latest ${formatAxisValue(latest, options)}, range ${formatAxisValue(Math.min(...numeric), options)} to ${formatAxisValue(Math.max(...numeric), options)}`;
  });
  canvas.setAttribute("aria-label", `${summaries.join(". ")}. ${state.history.length} session sample${state.history.length === 1 ? "" : "s"}.`);
}

function valuesToPoints(values, width, height, pad, min, max) {
  const plotWidth = width - pad.left - pad.right;
  const plotHeight = height - pad.top - pad.bottom;
  const count = Math.max(1, values.length - 1);
  return values.map((value, index) => {
    if (value === null || !Number.isFinite(value)) return null;
    const x = pad.left + (plotWidth * index) / count;
    const y = pad.top + plotHeight - ((value - min) / (max - min)) * plotHeight;
    return { x, y };
  });
}

function fillUnderLine(ctx, points, bottom, fillStyle) {
  const valid = points.filter(Boolean);
  if (valid.length < 2) return;
  ctx.beginPath();
  ctx.moveTo(valid[0].x, bottom);
  for (const point of valid) {
    ctx.lineTo(point.x, point.y);
  }
  ctx.lineTo(valid[valid.length - 1].x, bottom);
  ctx.closePath();
  ctx.fillStyle = fillStyle;
  ctx.fill();
}

function strokeLine(ctx, points, color, dashed, width) {
  ctx.save();
  ctx.beginPath();
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  if (dashed) {
    ctx.setLineDash([6, 6]);
  }
  let started = false;
  for (const point of points) {
    if (!point) {
      started = false;
      continue;
    }
    if (!started) {
      ctx.moveTo(point.x, point.y);
      started = true;
    } else {
      ctx.lineTo(point.x, point.y);
    }
  }
  ctx.stroke();
  ctx.restore();
}

function drawGrid(ctx, width, height, pad, min, max, options) {
  const left = pad.left;
  const right = width - pad.right;
  const top = pad.top;
  const bottom = height - pad.bottom;
  const ticks = options.ticks || 4;
  ctx.save();
  ctx.strokeStyle = palette.grid;
  ctx.lineWidth = 1;
  ctx.setLineDash([4, 4]);
  ctx.fillStyle = palette.muted;
  ctx.font = "12px system-ui, sans-serif";
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  for (let index = 0; index <= ticks; index += 1) {
    const y = top + ((bottom - top) * index) / ticks;
    ctx.beginPath();
    ctx.moveTo(left, y);
    ctx.lineTo(right, y);
    ctx.stroke();
    const value = max - ((max - min) * index) / ticks;
    ctx.fillText(formatAxisValue(value, options), left - 8, y);
  }
  ctx.restore();
  ctx.fillStyle = palette.muted;
  ctx.font = "12px system-ui, sans-serif";
  ctx.textAlign = "left";
  ctx.textBaseline = "alphabetic";
  const first = state.history[0];
  const last = state.history[state.history.length - 1];
  if (first && last) {
    ctx.fillText(shortTime(first.at), left, height - 8);
    ctx.textAlign = "right";
    ctx.fillText(shortTime(last.at), right, height - 8);
  }
}

function drawEmptyPlot(ctx, width, height, label) {
  ctx.fillStyle = palette.muted;
  ctx.font = "13px system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(label, width / 2, height / 2);
}

function prepareCanvas(canvas) {
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const cssWidth = Math.max(80, Math.round(rect.width));
  const cssHeight = Math.max(40, Math.round(rect.height));
  const nextWidth = Math.round(cssWidth * dpr);
  const nextHeight = Math.round(cssHeight * dpr);
  if (canvas.width !== nextWidth || canvas.height !== nextHeight) {
    canvas.width = nextWidth;
    canvas.height = nextHeight;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, width: cssWidth, height: cssHeight };
}

function eventErrorRate(events) {
  if (!Array.isArray(events) || events.length === 0) return 0;
  const bad = events.filter((event) => severityForEvent(event) === "ALERT").length;
  return (bad / events.length) * 100;
}

function resourceHasPositiveValue(value) {
  if (!value || typeof value !== "object") return false;
  return Object.values(value).some((item) => Number(item) > 0);
}

function setText(id, value) {
  const element = els[id] || document.getElementById(id);
  if (!element) return;
  els[id] = element;
  element.textContent = value;
}

function setNavBadge(id, value, mode = "") {
  const element = els[id] || document.getElementById(id);
  if (!element) return;
  element.textContent = typeof value === "number" ? formatInteger(value) : String(value);
  element.className = `nav-badge ${mode ? `nav-badge-${mode}` : ""}`.trim();
}

function setMeterWidth(id, value) {
  const element = els[id] || document.getElementById(id);
  if (!element) return;
  const percent = nullableNumber(value);
  element.style.width = `${Math.max(0, Math.min(100, percent === null ? 0 : percent))}%`;
}

function plainObject(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function firstText(...values) {
  for (const value of values) {
    if (value === null || value === undefined) continue;
    const text = String(value).trim();
    if (text) return text;
  }
  return "";
}

function asNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : 0;
}

function nullableNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function firstNumber(...values) {
  for (const value of values) {
    const number = nullableNumber(value);
    if (number !== null) return number;
  }
  return null;
}

function ratioToPercent(value) {
  const number = nullableNumber(value);
  return number === null ? null : number * 100;
}

function msToSeconds(value) {
  const number = nullableNumber(value);
  return number === null ? null : number / 1000;
}

function average(values) {
  const numeric = values.filter((value) => value !== null && Number.isFinite(value));
  if (numeric.length === 0) return null;
  return numeric.reduce((total, value) => total + value, 0) / numeric.length;
}

function formatInteger(value) {
  const number = nullableNumber(value);
  if (number === null) return "-";
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 }).format(number);
}

function formatNumber(value) {
  const number = nullableNumber(value);
  if (number === null) return "-";
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(number);
}

function formatAxisValue(value, options) {
  const number = nullableNumber(value);
  if (number === null) return "-";
  if (options.suffix === "%") return `${Math.round(number)}%`;
  if (options.integerAxis || Math.abs(number) >= 100) return formatInteger(number);
  if (Math.abs(number) >= 10) return number.toFixed(1);
  return number.toFixed(2).replace(/\.00$/, "");
}

function formatPercentPoint(value) {
  const number = nullableNumber(value);
  if (number === null) return "-";
  return `${Math.round(number)}%`;
}

function formatPercentDecimal(value) {
  const number = nullableNumber(value);
  if (number === null) return "-";
  return `${number.toFixed(2)}%`;
}

function formatDurationMs(value) {
  const number = nullableNumber(value);
  if (number === null) return "-";
  return formatAge(number / 1000);
}

function formatAge(value) {
  const seconds = nullableNumber(value);
  if (seconds === null) return "-";
  if (seconds < 1) return "<1s";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${Math.round(seconds / 3600)}h`;
}

function formatResources(value) {
  if (!value || typeof value !== "object") return "0 vCPU";
  const cpu = formatNumber(value.vcpu || 0);
  const memory = formatMemory(value.memory_mb || 0);
  const disk = asNumber(value.disk_mb);
  return `${cpu} vCPU / ${memory}${disk > 0 ? ` / ${formatMemory(disk)} disk` : ""}`;
}

function resourcePercent(used, total, key) {
  const numerator = nullableNumber((used || {})[key]);
  const denominator = nullableNumber((total || {})[key]);
  if (numerator === null || denominator === null || denominator <= 0) return null;
  return Math.max(0, Math.min(100, (numerator / denominator) * 100));
}

function formatMemory(value) {
  const number = nullableNumber(value);
  if (number === null) return "-";
  if (number >= 1024 * 1024) return `${formatNumber(number / (1024 * 1024))} TiB`;
  if (number >= 1024) return `${formatNumber(number / 1024)} GiB`;
  return `${formatNumber(number)} MiB`;
}

function formatTime(value) {
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return "-";
  return new Date(timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function shortTime(value) {
  return new Date(value).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
