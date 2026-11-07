"use strict";

(() => {
  const POLL_STATUS_MS = 2000;
  const POLL_SCREEN_MS = 1000;
  const STATUS_TIMEOUT_MS = 10000;
  const APPS_TIMEOUT_MS = 75000;
  const SCREEN_TIMEOUT_MS = 12000;
  const ACTION_TIMEOUT_MS = 30000;
  const FOLD_TIMEOUT_MS = 90000;
  const LAUNCH_TIMEOUT_MS = 150000;
  const UNLOCK_TIMEOUT_MS = 150000;
  const INSTALL_TIMEOUT_MS = 600000;
  const STOP_TIMEOUT_MS = 150000;
  const TAP_SLOP_PX = 8;

  const byId = (id) => document.getElementById(id);
  const tokenMeta = document.querySelector('meta[name="limulator-token"]');
  const mutationToken = tokenMeta?.content ?? "";

  const els = {
    connectionStatus: byId("connection-status"),
    connectionLabel: byId("connection-label"),
    deviceNameChip: byId("device-name-chip"),
    deviceName: byId("device-name"),
    deviceStateBadge: byId("device-state-badge"),
    detailModel: byId("detail-model"),
    detailOs: byId("detail-os"),
    detailResolution: byId("detail-resolution"),
    detailScale: byId("detail-scale"),
    detailFoldAngle: byId("detail-fold-angle"),
    foldAngleRow: byId("fold-angle-row"),
    detailRuntime: byId("detail-runtime"),
    deviceError: byId("device-error"),
    screenEmpty: byId("screen-empty"),
    screenEmptyTitle: byId("screen-empty-title"),
    screenEmptyCopy: byId("screen-empty-copy"),
    screenImage: byId("screen-image"),
    screenResolution: byId("screen-resolution"),
    screenLiveIndicator: byId("screen-live-indicator"),
    downloadScreen: byId("download-screen"),
    textForm: byId("text-form"),
    deviceText: byId("device-text"),
    sendText: byId("send-text"),
    appSelector: byId("app-selector"),
    appsHint: byId("apps-hint"),
    refreshApps: byId("refresh-apps"),
    launchApp: byId("launch-app"),
    ipaFile: byId("ipa-file"),
    chooseIpa: byId("choose-ipa"),
    selectedFile: byId("selected-file"),
    notice: byId("notice"),
    noticeMessage: byId("notice-message"),
    dismissNotice: byId("dismiss-notice"),
    lastUpdated: byId("last-updated"),
    deviceSelector: byId("device-selector"),
    destinationSummary: byId("destination-summary"),
    profileFacts: byId("profile-facts"),
    profileSetup: byId("profile-setup"),
    profileSetupTitle: byId("profile-setup-title"),
    profileSetupCopy: byId("profile-setup-copy"),
    createCommand: byId("create-command"),
    copyCreateCommand: byId("copy-create-command"),
    refreshDevices: byId("refresh-devices"),
    startDevice: byId("start-device"),
    stopDevice: byId("stop-device"),
    unlockDevice: byId("unlock-device"),
    foldHeading: byId("fold-heading"),
    foldControls: byId("fold-controls"),
    foldUnavailable: byId("fold-unavailable"),
  };

  let deviceStatus = null;
  let statusError = "";
  let screenError = "";
  let screenFrameId = "";
  let appError = "";
  let installedApps = [];
  let appsLoading = false;
  let mutationInFlight = "";
  let screenObjectUrl = "";
  let statusTimer = null;
  let screenTimer = null;
  let noticeTimer = null;
  let statusInFlight = false;
  let screenInFlight = false;
  let statusRequest = { current: null };
  let screenRequest = { current: null };
  let lastAppsDeviceKey = "";
  let pointerStart = null;
  let catalog = { devices: [], profiles: [], default_device: "duo" };
  let selectedName = "";
  let selectedProfile = null;
  let selectionEpoch = 0;
  let catalogInFlight = false;
  let appsRequest = { current: null };
  const operations = new Map();
  const pendingReads = new Map();

  function selectionIsCurrent(name, epoch) {
    return selectedName === name && selectionEpoch === epoch;
  }

  function deviceUrl(path, name = selectedName) {
    const url = new URL(path, window.location.origin);
    if (name) url.searchParams.set("device", name);
    return url.pathname + url.search;
  }

  function selectedMetadata() {
    return catalog.devices.find((device) => device.name === selectedName) || null;
  }

  function supportsFold() {
    return selectedMetadata()?.supports_fold === true;
  }

  function syncOperation() {
    mutationInFlight = operations.get(selectedName)?.path || "";
  }

  function resetPolling() {
    selectionEpoch++;
    statusRequest = { current: null };
    screenRequest = { current: null };
    appsRequest = { current: null };
    window.clearTimeout(statusTimer);
    window.clearTimeout(screenTimer);
    statusInFlight = screenInFlight = appsLoading = false;
    pointerStart = null;
    clearScreen();
  }

  function stringValue(value) {
    return typeof value === "string" ? value.trim() : "";
  }

  function displayValue(value, suffix = "") {
    if (value === null || value === undefined || value === "") return "N/A";
    return `${value}${suffix}`;
  }

  function operatingSystem(profile, model = "") {
    return profile?.category === "tablet" || model.startsWith("iPad") ? "iPadOS" : "iOS";
  }

  function classifyState(value) {
    const state = stringValue(value).toLowerCase().replaceAll("_", "-");
    if (!state) return "unknown";
    if (state === "stopping") return "stopping";
    if (/error|failed|failure/.test(state)) return "error";
    if (/offline|stopp|disconnect|powered-off|not-(ready|running|connected)|down/.test(state)) return "offline";
    if (/connected|online|ready|running|booted|active/.test(state)) return "connected";
    if (/booting|starting|launching|initializ|preparing|warming/.test(state)) return "booting";
    return "unknown";
  }

  function stateLabel(category) {
    if (category === "connected") return "Connected";
    if (category === "booting") return "Booting";
    if (category === "stopping") return "Stopping";
    if (category === "offline") return "Offline";
    if (category === "unconfigured") return "Not created";
    if (category === "error") return statusError ? "Unavailable" : "Error";
    const raw = stringValue(deviceStatus?.state);
    return raw ? raw.replaceAll("_", " ") : "Unknown";
  }

  function currentCategory() {
    if (!selectedName && selectedProfile) return "unconfigured";
    if (mutationInFlight === "/api/stop") return "stopping";
    if (mutationInFlight === "/api/start") return "booting";
    if (statusError) return "error";
    return classifyState(deviceStatus?.state);
  }

  function deviceConnected() {
    return currentCategory() === "connected";
  }

  function updateStatusBadge() {
    const category = currentCategory();
    els.connectionStatus.className = `connection-status status-${category}`;
    els.deviceStateBadge.className = `state-badge status-${category}`;
    const label = stateLabel(category);
    els.connectionLabel.textContent = label;
    els.deviceStateBadge.textContent = label;
  }

  function updateEmptyScreen() {
    if (!els.screenImage.hidden) return;

    if (screenError && deviceConnected()) {
      els.screenEmptyTitle.textContent = "Screen unavailable";
      els.screenEmptyCopy.textContent = screenError;
      return;
    }

    const category = currentCategory();
    if (category === "connected") {
      els.screenEmptyTitle.textContent = "Waiting for live screen";
      els.screenEmptyCopy.textContent = "The device is connected. Its actual display will appear as soon as a screenshot is available.";
    } else if (category === "booting") {
      els.screenEmptyTitle.textContent = "Device is booting";
      els.screenEmptyCopy.textContent = "The live screen will appear when the device is ready.";
    } else if (category === "stopping") {
      els.screenEmptyTitle.textContent = "Shutting down safely";
      els.screenEmptyCopy.textContent = `Waiting for ${selectedName} to finish shutting down.`;
    } else if (!selectedName && selectedProfile) {
      els.screenEmptyTitle.textContent = `${selectedProfile.label} is ready to create`;
      els.screenEmptyCopy.textContent = "Use the setup command above to prepare a local device. Its real display will appear here after startup.";
    } else if (category === "offline") {
      els.screenEmptyTitle.textContent = "Device is offline";
      els.screenEmptyCopy.textContent = "Start the device to view its actual display here.";
    } else if (category === "error") {
      els.screenEmptyTitle.textContent = statusError ? "Device status unavailable" : "Device runtime error";
      els.screenEmptyCopy.textContent = statusError || stringValue(deviceStatus?.error) || "The device runtime reported an error.";
    } else {
      els.screenEmptyTitle.textContent = "No live screen yet";
      els.screenEmptyCopy.textContent = "Connect and boot a device to see its actual display here.";
    }
  }

  function setDeviceError(message) {
    const error = stringValue(message);
    if (error) {
      els.deviceError.textContent = error;
      els.deviceError.hidden = false;
    } else {
      els.deviceError.textContent = "";
      els.deviceError.hidden = true;
    }
  }

  function formatResolution(width, height) {
    const w = Number(width);
    const h = Number(height);
    return Number.isFinite(w) && w > 0 && Number.isFinite(h) && h > 0 ? `${w} × ${h}` : "N/A";
  }

  function foldPosition(angle) {
    const value = Number(angle);
    if (!Number.isFinite(value)) return "";
    if (value <= 15) return "closed";
    if (value >= 150) return "open";
    if (value >= 40 && value <= 140) return "book";
    return "";
  }

  function updateFoldState() {
    const angle = deviceStatus?.device?.fold_angle;
    const hasAngle = angle !== null && angle !== undefined && Number.isFinite(Number(angle));
    els.foldAngleRow.hidden = !hasAngle;
    els.detailFoldAngle.textContent = hasAngle ? `${Number(angle).toFixed(0)}°` : "N/A";

    const active = foldPosition(angle);
    document.querySelectorAll("[data-fold-position]").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.foldPosition === active && Boolean(active)));
    });
  }

  function updateDeviceFacts() {
    const device = deviceStatus?.device ?? selectedMetadata() ?? selectedProfile ?? {};
    const model = stringValue(device.profile?.label) || stringValue(selectedProfile?.label) || stringValue(device.model);
    const name = selectedName || stringValue(selectedProfile?.label);
    els.deviceName.textContent = name || "Device";
    els.deviceNameChip.hidden = !name;
    els.detailModel.textContent = displayValue(model);
    els.detailOs.textContent = device.os_version
      ? `${operatingSystem(selectedProfile || device.profile, device.model)} ${device.os_version}`
      : "N/A";
    els.detailResolution.textContent = formatResolution(device.width, device.height);
    els.detailScale.textContent = device.scale === null || device.scale === undefined ? "N/A" : `${device.scale}×`;
    updateFoldState();

    const backendReady = deviceStatus?.backend?.ready;
    if (!selectedName && selectedProfile) {
      els.detailRuntime.textContent = "Not created";
    } else if (statusError) {
      els.detailRuntime.textContent = "Unavailable";
    } else if (backendReady === true) {
      els.detailRuntime.textContent = "Ready";
    } else if (backendReady === false) {
      els.detailRuntime.textContent = "Not ready";
    } else {
      els.detailRuntime.textContent = "Unknown";
    }

    const apiError = stringValue(deviceStatus?.error);
    setDeviceError(statusError || apiError);
  }

  function updateScreenResolution() {
    if (!els.screenImage.hidden && els.screenImage.naturalWidth > 0 && els.screenImage.naturalHeight > 0) {
      els.screenResolution.textContent = `${els.screenImage.naturalWidth} × ${els.screenImage.naturalHeight}`;
    } else {
      els.screenResolution.textContent = deviceConnected() ? "Waiting for screen" : "No screen";
    }
  }

  function updateAppsView() {
    const previous = els.appSelector.value;
    els.appSelector.replaceChildren();

    if (appsLoading && installedApps.length === 0) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "Loading installed apps…";
      els.appSelector.append(option);
    } else if (installedApps.length === 0) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = appError ? "Could not load installed apps" : "No installed apps found";
      els.appSelector.append(option);
    } else {
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = "Choose an installed app…";
      els.appSelector.append(placeholder);

      for (const app of installedApps) {
        const option = document.createElement("option");
        option.value = app.bundle_id;
        option.textContent = app.name ? `${app.name} · ${app.bundle_id}` : app.bundle_id;
        els.appSelector.append(option);
      }
      if (installedApps.some((app) => app.bundle_id === previous)) els.appSelector.value = previous;
    }

    if (appError) {
      els.appsHint.textContent = appError;
    } else if (!deviceConnected()) {
      els.appsHint.textContent = "Connect a device to view its installed apps.";
    } else if (installedApps.length === 0 && !appsLoading) {
      els.appsHint.textContent = "No installed apps were reported by the device.";
    } else {
      els.appsHint.textContent = "Apps are reported by the connected device.";
    }
  }

  function updateControls() {
    const connected = deviceConnected();
    const busy = Boolean(mutationInFlight);
    document.querySelectorAll('[data-action="button"]').forEach((button) => {
      button.disabled = !connected || busy;
    });
    document.querySelectorAll("[data-fold-position]").forEach((button) => {
      button.disabled = !connected || busy || !supportsFold();
    });
    els.foldHeading.hidden = !supportsFold();
    els.foldControls.hidden = !supportsFold();
    els.foldUnavailable.hidden = supportsFold();
    const category = currentCategory();
    els.startDevice.textContent = mutationInFlight === "/api/start" ? `Starting ${selectedName}…` : `Start ${selectedName || "device"}`;
    els.stopDevice.textContent = mutationInFlight === "/api/stop" || category === "stopping" ? `Stopping ${selectedName}…` : `Stop ${selectedName || "device"}`;
    els.startDevice.disabled = !selectedName || busy || !["offline", "error"].includes(category);
    els.stopDevice.disabled = !selectedName || busy || !["connected", "booting"].includes(category);
    els.unlockDevice.disabled = !connected || busy;
    els.deviceText.disabled = !connected || busy;
    els.sendText.disabled = !connected || busy;
    els.chooseIpa.disabled = !connected || busy;
    els.ipaFile.disabled = !connected || busy;
    els.appSelector.disabled = !connected || busy || appsLoading || installedApps.length === 0;
    els.launchApp.disabled = !connected || busy || !els.appSelector.value;
    els.refreshApps.disabled = !connected || appsLoading || busy;
    els.downloadScreen.disabled = els.screenImage.hidden;
    els.screenLiveIndicator.hidden = els.screenImage.hidden;
    els.lastUpdated.textContent = deviceStatus && !statusError
      ? `Status updated ${new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" })}`
      : statusError ? "Unable to refresh device status" : "Waiting for device status";
  }

  function renderStatus() {
    updateStatusBadge();
    updateDeviceFacts();
    updateScreenResolution();
    updateEmptyScreen();
    updateAppsView();
    updateControls();
  }

  function showNotice(message, isError = false, timeoutMs = 6000) {
    window.clearTimeout(noticeTimer);
    els.noticeMessage.textContent = message;
    els.notice.classList.toggle("notice-error", isError);
    els.notice.hidden = !message;
    if (message && timeoutMs > 0) {
      noticeTimer = window.setTimeout(() => {
        els.notice.hidden = true;
      }, timeoutMs);
    }
  }

  function readErrorMessage(body, response) {
    if (body && typeof body === "object") {
      for (const key of ["detail", "error", "message"]) {
        const value = body[key];
        if (typeof value === "string" && value.trim()) return value.trim();
      }
    } else if (typeof body === "string" && body.trim()) {
      return body.trim();
    }
    const statusText = stringValue(response.statusText);
    return statusText ? `Request failed: ${response.status} ${statusText}` : `Request failed with HTTP ${response.status}`;
  }

  function requestError(error, request) {
    if (error?.name === "AbortError" && request?.timedOut) {
      const duration = request.timeoutMs < 1000
        ? `${request.timeoutMs} milliseconds` : `${Math.round(request.timeoutMs / 1000)} seconds`;
      return new Error(`Request timed out after ${duration}.`);
    }
    return error;
  }

  async function responseBody(response, requestRef) {
    const request = requestRef.current;
    try {
      const text = await response.text();
      if (!text) return null;
      try {
        return JSON.parse(text);
      } catch {
        return text;
      }
    } catch (error) {
      throw requestError(error, request);
    } finally {
      releaseRequest(requestRef, request);
    }
  }

  async function fetchWithTimeout(url, options, timeoutMs, requestRef) {
    const controller = new AbortController();
    const request = { controller, timeoutMs, timedOut: false, timeout: null };
    request.timeout = window.setTimeout(() => {
      request.timedOut = true;
      controller.abort();
    }, timeoutMs);
    requestRef.current = request;
    try {
      return await fetch(url, { cache: "no-store", ...options, signal: controller.signal });
    } catch (error) {
      releaseRequest(requestRef, request);
      throw requestError(error, request);
    }
  }

  function releaseRequest(requestRef, request = requestRef?.current) {
    if (!request) return;
    window.clearTimeout(request.timeout);
    if (requestRef?.current === request) requestRef.current = null;
  }

  function beginRead(name) {
    const reads = pendingReads.get(name) || new Set();
    pendingReads.set(name, reads);
    let finish;
    const completion = new Promise((resolve) => { finish = resolve; });
    reads.add(completion);
    return (error = null) => {
      finish(error);
      reads.delete(completion);
      if (pendingReads.get(name) === reads && reads.size === 0) pendingReads.delete(name);
    };
  }

  function scheduleStatus(delay = POLL_STATUS_MS) {
    window.clearTimeout(statusTimer);
    if (document.hidden || operations.has(selectedName)) return;
    statusTimer = window.setTimeout(pollStatus, delay);
  }

  function scheduleScreen(delay = POLL_SCREEN_MS) {
    window.clearTimeout(screenTimer);
    if (document.hidden || operations.has(selectedName)) return;
    screenTimer = window.setTimeout(pollScreen, delay);
  }

  function deviceAppsKey() {
    return selectedName;
  }

  function updateDestination() {
    const profile = selectedProfile;
    const metadata = selectedMetadata();
    els.profileFacts.replaceChildren();
    const category = { phone: "iPhone", tablet: "iPad", foldable: "Foldable" }[profile?.category] || "Custom device";
    const facts = [category, `${operatingSystem(profile, metadata?.model)} ${profile?.os_version || metadata?.os_version || "N/A"}`];
    if (profile || metadata) {
      facts.push(`${formatResolution((profile || metadata).width, (profile || metadata).height)} px`);
      facts.push(`${(profile || metadata).scale}× scale`);
    }
    for (const text of facts) {
      const fact = document.createElement("span");
      fact.className = "profile-fact";
      fact.textContent = text;
      els.profileFacts.append(fact);
    }
    const support = document.createElement("span");
    support.className = `profile-fact ${profile?.upstream_linux_tested ? "support" : "pending"}`;
    support.textContent = profile?.upstream_linux_tested ? "Upstream Linux tested" : "Runtime verification pending";
    els.profileFacts.append(support);
    els.destinationSummary.textContent = selectedName
      ? `Local device · ${selectedName} · ${metadata?.model || ""}`
      : "Available profile · create a local device to run apps";
    els.profileSetup.hidden = Boolean(selectedName) || !profile;
    if (!selectedName && profile) {
      let name = catalog.devices.length === 0 && profile.id === "iphone-duo" ? catalog.default_device : profile.id;
      const base = name;
      for (let suffix = 2; catalog.devices.some((device) => device.name === name); suffix++) name = `${base}-${suffix}`;
      els.profileSetupTitle.textContent = `Create ${profile.label}`;
      els.profileSetupCopy.textContent = profile.firmware_metadata_ready
        ? "Run this command in your terminal. The creator prepares the local firmware and preserves its progress. Refresh this list after creation."
        : "This profile is listed for planning. Its firmware metadata must be completed before device creation can run.";
      els.createCommand.value = `uv run limulator --device ${name} create --profile ${profile.id}`;
      els.copyCreateCommand.disabled = !profile.firmware_metadata_ready;
    }
  }

  function selectDestination(value) {
    const name = value.startsWith("device:") ? value.slice(7) : "";
    const metadata = catalog.devices.find((device) => device.name === name);
    const profile = metadata?.profile || catalog.profiles.find((item) => value === `profile:${item.id}`) || null;
    const changed = name !== selectedName || profile?.id !== selectedProfile?.id;
    selectedName = name;
    selectedProfile = profile;
    els.deviceSelector.value = value;
    if (changed) {
      resetPolling();
      deviceStatus = null;
      statusError = screenError = appError = "";
      installedApps = [];
      lastAppsDeviceKey = "";
      pointerStart = null;
      syncOperation();
      clearScreen();
      showNotice("");
    }
    updateDestination();
    renderStatus();
    if (selectedName) {
      scheduleStatus(0);
      scheduleScreen(0);
    }
  }

  async function loadDevices() {
    if (catalogInFlight) return;
    catalogInFlight = true;
    els.refreshDevices.disabled = true;
    const requestRef = { current: null };
    try {
      const response = await fetchWithTimeout("/api/devices", {}, STATUS_TIMEOUT_MS, requestRef);
      const body = await responseBody(response, requestRef);
      if (!response.ok) throw new Error(readErrorMessage(body, response));
      if (!Array.isArray(body?.devices) || !Array.isArray(body?.profiles)) throw new Error("Device catalog is unavailable.");
      const previous = els.deviceSelector.value;
      catalog = body;
      els.deviceSelector.replaceChildren();
      const registered = document.createElement("optgroup");
      registered.label = "My devices";
      for (const device of catalog.devices) {
        const option = document.createElement("option");
        option.value = `device:${device.name}`;
        option.textContent = `${device.profile?.label || device.model} · ${device.name} · ${operatingSystem(device.profile, device.model)} ${device.os_version}`;
        registered.append(option);
      }
      if (registered.children.length) els.deviceSelector.append(registered);
      const available = document.createElement("optgroup");
      available.label = "Create a device";
      for (const profile of catalog.profiles) {
        if (catalog.devices.some((device) => device.profile?.id === profile.id)) continue;
        const option = document.createElement("option");
        option.value = `profile:${profile.id}`;
        option.textContent = `${profile.label} · ${operatingSystem(profile, profile.model)} ${profile.os_version}`;
        available.append(option);
      }
      if (available.children.length) els.deviceSelector.append(available);
      const options = Array.from(els.deviceSelector.options).map((option) => option.value);
      const preferred = `device:${catalog.default_device}`;
      selectDestination(options.includes(previous) ? previous : options.includes(preferred) ? preferred : options[0] || "");
    } catch (error) {
      showNotice(error instanceof Error ? error.message : String(error), true, 0);
      els.destinationSummary.textContent = "Could not read the local device registry. Refresh to retry.";
    } finally {
      catalogInFlight = false;
      els.refreshDevices.disabled = false;
    }
  }

  async function pollStatus() {
    if (document.hidden || statusInFlight || !selectedName || operations.has(selectedName)) return;
    const name = selectedName;
    const epoch = selectionEpoch;
    const requestRef = statusRequest;
    const finishRead = beginRead(name);
    let readError;
    statusInFlight = true;
    const previousCategory = currentCategory();
    const previousAppsKey = deviceAppsKey();
    try {
      const response = await fetchWithTimeout(deviceUrl("/api/status", name), {}, STATUS_TIMEOUT_MS, requestRef);
      const body = await responseBody(response, requestRef);
      if (!selectionIsCurrent(name, epoch)) return;
      if (!response.ok) throw new Error(readErrorMessage(body, response));
      if (!body || typeof body !== "object") throw new Error("The status endpoint returned an invalid response.");
      deviceStatus = body;
      statusError = "";
      screenError = "";
    } catch (error) {
      readError = error;
      if (!selectionIsCurrent(name, epoch)) return;
      if (!(document.hidden && error?.name === "AbortError")) {
        statusError = error instanceof Error ? error.message : String(error);
        deviceStatus = null;
        clearScreen();
      }
    } finally {
      finishRead(readError);
      if (!selectionIsCurrent(name, epoch)) return;
      statusInFlight = false;
      renderStatus();
      const category = currentCategory();
      if (category === "connected" && (previousCategory !== "connected" || previousAppsKey !== deviceAppsKey())) {
        lastAppsDeviceKey = deviceAppsKey();
        void loadApps(false);
      } else if (category !== "connected") {
        lastAppsDeviceKey = "";
      }
      scheduleStatus();
    }
  }

  function clearScreen() {
    if (screenObjectUrl) {
      URL.revokeObjectURL(screenObjectUrl);
      screenObjectUrl = "";
    }
    els.screenImage.removeAttribute("src");
    els.screenImage.hidden = true;
    els.screenEmpty.hidden = false;
    els.screenLiveIndicator.hidden = true;
    screenFrameId = "";
    updateScreenResolution();
    updateEmptyScreen();
    updateControls();
  }

  async function decodeImage(url) {
    const image = new Image();
    await new Promise((resolve, reject) => {
      image.onload = resolve;
      image.onerror = () => reject(new Error("The screen response was not a readable image."));
      image.src = url;
    });
    return { width: image.naturalWidth, height: image.naturalHeight };
  }

  async function pollScreen() {
    if (document.hidden || screenInFlight || !selectedName || operations.has(selectedName)) return;
    const name = selectedName;
    const epoch = selectionEpoch;
    const requestRef = screenRequest;
    const finishRead = beginRead(name);
    let readError;
    screenInFlight = true;
    try {
      if (!deviceConnected()) {
        screenError = "";
        clearScreen();
        return;
      }

      const response = await fetchWithTimeout(deviceUrl(`/api/screen.png?ts=${Date.now()}`, name), {}, SCREEN_TIMEOUT_MS, requestRef);
      let nextFrameId;
      let blob;
      try {
        const body = response.ok ? null : await responseBody(response, requestRef);
        if (!response.ok) throw new Error(readErrorMessage(body, response));
        nextFrameId = response.headers.get("X-Limulator-Frame") || "";
        blob = await response.blob();
        const signature = new Uint8Array(await blob.slice(0, 8).arrayBuffer());
        const pngSignature = [137, 80, 78, 71, 13, 10, 26, 10];
        if (signature.length !== pngSignature.length || !pngSignature.every((byte, index) => signature[index] === byte)) {
          throw new Error("The screen endpoint did not return a PNG image.");
        }
      } catch (error) {
        throw requestError(error, requestRef.current);
      } finally {
        releaseRequest(requestRef);
      }
      finishRead();
      if (!selectionIsCurrent(name, epoch)) return;
      const nextUrl = URL.createObjectURL(blob);
      try {
        const dimensions = await decodeImage(nextUrl);
        if (!selectionIsCurrent(name, epoch) || document.hidden || !deviceConnected()) {
          URL.revokeObjectURL(nextUrl);
          return;
        }

        const oldUrl = screenObjectUrl;
        screenObjectUrl = nextUrl;
        screenFrameId = nextFrameId;
        els.screenImage.src = nextUrl;
        els.screenImage.alt = `Live device screen preview, ${dimensions.width} by ${dimensions.height} pixels`;
        els.screenImage.hidden = false;
        els.screenEmpty.hidden = true;
        screenError = "";
        updateScreenResolution();
        updateControls();
        if (oldUrl) URL.revokeObjectURL(oldUrl);
      } catch (error) {
        URL.revokeObjectURL(nextUrl);
        throw error;
      }
    } catch (error) {
      readError = error;
      if (!selectionIsCurrent(name, epoch)) return;
      if (deviceConnected() && !(document.hidden && error?.name === "AbortError")) {
        screenError = error instanceof Error ? error.message : String(error);
        clearScreen();
      }
    } finally {
      finishRead(readError);
      if (!selectionIsCurrent(name, epoch)) return;
      screenInFlight = false;
      scheduleScreen();
    }
  }

  async function loadApps(showErrorNotice = true) {
    if (appsLoading || !selectedName || !deviceConnected() || operations.has(selectedName)) return;
    const name = selectedName;
    const epoch = selectionEpoch;
    const requestRef = appsRequest;
    const finishRead = beginRead(name);
    let readError;
    appsLoading = true;
    appError = "";
    updateAppsView();
    updateControls();
    try {
      const response = await fetchWithTimeout(deviceUrl("/api/apps", name), {}, APPS_TIMEOUT_MS, requestRef);
      const body = await responseBody(response, requestRef);
      if (!selectionIsCurrent(name, epoch)) return;
      if (!response.ok) throw new Error(readErrorMessage(body, response));
      if (!body || !Array.isArray(body.apps)) throw new Error("The apps endpoint did not return an apps list.");
      installedApps = body.apps
        .filter((app) => app && typeof app.bundle_id === "string" && app.bundle_id.trim())
        .map((app) => ({ bundle_id: app.bundle_id.trim(), name: stringValue(app.name) }));
    } catch (error) {
      readError = error;
      if (!selectionIsCurrent(name, epoch)) return;
      installedApps = [];
      appError = error instanceof Error ? error.message : String(error);
      if (showErrorNotice) showNotice(appError, true);
    } finally {
      finishRead(readError);
      if (!selectionIsCurrent(name, epoch)) return;
      appsLoading = false;
      updateAppsView();
      updateControls();
    }
  }

  function mutationHeaders() {
    if (!mutationToken || mutationToken === "__LIMULATOR_TOKEN__") {
      throw new Error("The Limulator session token has not been configured.");
    }
    return { "X-Limulator-Token": mutationToken };
  }

  async function mutate(path, {
    body,
    file,
    timeoutMs = ACTION_TIMEOUT_MS,
    pendingMessage = "Sending device command…",
    successMessage = "Request completed.",
    refreshAppsAfter = false,
    lifecycle = false,
  } = {}) {
    if (!selectedName || (!lifecycle && !deviceConnected())) {
      showNotice("Connect a device before sending commands.", true);
      return false;
    }
    const name = selectedName;
    if (operations.has(name)) return false;

    let headers;
    try {
      headers = mutationHeaders();
    } catch (error) {
      showNotice(error.message, true);
      return false;
    }

    const operation = { path };
    const requestRef = { current: null };
    operations.set(name, operation);
    syncOperation();
    window.clearTimeout(statusTimer);
    window.clearTimeout(screenTimer);
    let epoch = selectionEpoch;
    let refreshApps = false;
    updateControls();
    showNotice(pendingMessage, false, 0);
    try {
      // Aborting fetch does not stop a capture already running on the server.
      const readErrors = await Promise.all(pendingReads.get(name) || []);
      const readError = readErrors.find(Boolean);
      if (readError) throw readError;
      if (lifecycle && selectionIsCurrent(name, epoch)) {
        resetPolling();
        epoch = selectionEpoch;
      }
      let requestBody;
      if (file) {
        requestBody = new FormData();
        requestBody.append("file", file, file.name);
      } else {
        headers["Content-Type"] = "application/json";
        requestBody = JSON.stringify(body ?? {});
      }

      const response = await fetchWithTimeout(deviceUrl(path, name), {
        method: "POST",
        headers,
        body: requestBody,
      }, timeoutMs, requestRef);
      const result = await responseBody(response, requestRef);
      if (!response.ok) throw new Error(readErrorMessage(result, response));
      if (!selectionIsCurrent(name, epoch)) return true;
      if (lifecycle) deviceStatus = result;
      const serverMessage = result && typeof result === "object" ? stringValue(result.message) : "";
      showNotice(serverMessage || successMessage, false);
      refreshApps = refreshAppsAfter;
      return true;
    } catch (error) {
      if (selectionIsCurrent(name, epoch)) showNotice(error instanceof Error ? error.message : String(error), true);
      return false;
    } finally {
      if (operations.get(name) === operation) operations.delete(name);
      if (selectedName === name) {
        syncOperation();
        renderStatus();
        if (refreshApps && selectionIsCurrent(name, epoch)) void loadApps(false);
        scheduleStatus(0);
        scheduleScreen(0);
      }
    }
  }

  async function downloadScreen() {
    if (!screenObjectUrl || els.screenImage.hidden) return;
    const link = document.createElement("a");
    link.href = screenObjectUrl;
    link.download = `limulator-screen-${Date.now()}.png`;
    document.body.append(link);
    link.click();
    link.remove();
  }

  function pointForEvent(event, clampToEdge = false) {
    const rect = els.screenImage.getBoundingClientRect();
    if (!rect.width || !rect.height || !els.screenImage.naturalWidth || !els.screenImage.naturalHeight) return null;
    const localX = event.clientX - rect.left;
    const localY = event.clientY - rect.top;
    if (!clampToEdge && (localX < 0 || localY < 0 || localX > rect.width || localY > rect.height)) return null;
    const x = Math.min(els.screenImage.naturalWidth - 1, Math.max(0, Math.round(localX / rect.width * els.screenImage.naturalWidth)));
    const y = Math.min(els.screenImage.naturalHeight - 1, Math.max(0, Math.round(localY / rect.height * els.screenImage.naturalHeight)));
    return { x, y };
  }

  function onScreenPointerDown(event) {
    if (!deviceConnected() || mutationInFlight || event.button !== 0) return;
    const point = pointForEvent(event);
    if (!point) return;
    event.preventDefault();
    pointerStart = { ...point, frameId: screenFrameId, clientX: event.clientX, clientY: event.clientY, time: performance.now(), pointerId: event.pointerId };
    try {
      els.screenImage.setPointerCapture(event.pointerId);
    } catch {
      pointerStart = null;
    }
  }

  function onScreenPointerUp(event) {
    if (!pointerStart || pointerStart.pointerId !== event.pointerId) return;
    const start = pointerStart;
    pointerStart = null;
    if (start.frameId !== screenFrameId) {
      showNotice("The display changed during the gesture. Try again on the current frame.", true);
      return;
    }
    const end = pointForEvent(event, true);
    if (!end) return;

    const deltaX = event.clientX - start.clientX;
    const deltaY = event.clientY - start.clientY;
    if (Math.hypot(deltaX, deltaY) < TAP_SLOP_PX) {
      void mutate("/api/input", {
        body: { type: "tap", x: start.x, y: start.y, ...(screenFrameId ? { frame_id: screenFrameId } : {}) },
        pendingMessage: "Sending tap command…",
        successMessage: "Tap command completed.",
      });
    } else {
      void mutate("/api/input", {
        body: {
          type: "swipe",
          x0: start.x,
          y0: start.y,
          x1: end.x,
          y1: end.y,
          duration_ms: Math.min(5000, Math.max(50, Math.round(performance.now() - start.time))),
          ...(screenFrameId ? { frame_id: screenFrameId } : {}),
        },
        pendingMessage: "Sending swipe command…",
        successMessage: "Swipe command completed.",
      });
    }
  }

  function onScreenKeyDown(event) {
    if ((event.key === "Enter" || event.key === " ") && !event.repeat && deviceConnected() && !mutationInFlight && !els.screenImage.hidden) {
      event.preventDefault();
      void mutate("/api/input", {
        body: {
          type: "tap",
          x: Math.floor(els.screenImage.naturalWidth / 2),
          y: Math.floor(els.screenImage.naturalHeight / 2),
          ...(screenFrameId ? { frame_id: screenFrameId } : {}),
        },
        pendingMessage: "Sending tap command…",
        successMessage: "Tap command completed.",
      });
    }
  }

  function bindEvents() {
    els.deviceSelector.addEventListener("change", () => selectDestination(els.deviceSelector.value));
    els.refreshDevices.addEventListener("click", () => void loadDevices());
    els.startDevice.addEventListener("click", () => void mutate("/api/start", {
      lifecycle: true,
      timeoutMs: 20000,
      pendingMessage: `Starting ${selectedName}…`,
      successMessage: "Startup requested. The screen will appear when the guest is ready.",
    }));
    els.stopDevice.addEventListener("click", () => void mutate("/api/stop", {
      lifecycle: true,
      timeoutMs: STOP_TIMEOUT_MS,
      pendingMessage: `Shutting down ${selectedName} safely…`,
      successMessage: "The selected device has stopped.",
    }));
    els.unlockDevice.addEventListener("click", () => void mutate("/api/unlock", {
      timeoutMs: UNLOCK_TIMEOUT_MS,
      pendingMessage: "Unlocking device…",
      successMessage: "Unlock request completed.",
    }));
    els.copyCreateCommand.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(els.createCommand.value);
        showNotice("Device creation command copied.");
      } catch {
        els.createCommand.focus();
        els.createCommand.select();
        showNotice("Select and copy the command from the field.");
      }
    });
    els.dismissNotice.addEventListener("click", () => {
      window.clearTimeout(noticeTimer);
      els.notice.hidden = true;
    });

    els.downloadScreen.addEventListener("click", () => void downloadScreen());

    els.textForm.addEventListener("submit", (event) => {
      event.preventDefault();
      const epoch = selectionEpoch;
      const text = els.deviceText.value;
      if (!text.trim()) {
        showNotice("Enter text to send to the device.", true);
        return;
      }
      void mutate("/api/input", {
        body: { type: "text", text },
        pendingMessage: "Sending text to the device…",
        successMessage: "Text request completed.",
      }).then((completed) => {
        if (completed && selectionEpoch === epoch) els.deviceText.value = "";
      });
    });

    document.querySelectorAll('[data-action="button"]').forEach((button) => {
      button.addEventListener("click", () => {
        void mutate("/api/input", {
          body: { type: "button", name: button.dataset.name },
          pendingMessage: `Sending ${button.textContent.trim()} command…`,
          successMessage: `${button.textContent.trim()} request completed.`,
        });
      });
    });

    document.querySelectorAll("[data-fold-position]").forEach((button) => {
      button.addEventListener("click", () => {
        void mutate("/api/fold", {
          body: { position: button.dataset.foldPosition },
          timeoutMs: FOLD_TIMEOUT_MS,
          pendingMessage: "Changing fold posture…",
          successMessage: `Fold position request completed.`,
        });
      });
    });

    els.refreshApps.addEventListener("click", () => void loadApps(true));
    els.appSelector.addEventListener("change", updateControls);
    els.launchApp.addEventListener("click", () => {
      const bundleId = els.appSelector.value;
      if (!bundleId) return;
      void mutate("/api/launch", {
        body: { bundle_id: bundleId },
        timeoutMs: LAUNCH_TIMEOUT_MS,
        pendingMessage: "Sending launch request…",
        successMessage: "Launch request completed.",
        refreshAppsAfter: false,
      });
    });

    els.chooseIpa.addEventListener("click", () => els.ipaFile.click());
    els.ipaFile.addEventListener("change", () => {
      const epoch = selectionEpoch;
      const file = els.ipaFile.files?.[0];
      if (!file) return;
      els.selectedFile.textContent = file.name;
      if (!file.name.toLowerCase().endsWith(".ipa")) {
        showNotice("Choose a file with the .ipa extension.", true);
        els.ipaFile.value = "";
        els.selectedFile.textContent = "No file selected";
        return;
      }
      void mutate("/api/install", {
        file,
        timeoutMs: INSTALL_TIMEOUT_MS,
        pendingMessage: "Installing IPA…",
        successMessage: "Install request completed.",
        refreshAppsAfter: true,
      }).then((completed) => {
        if (completed && selectionEpoch === epoch) {
          els.ipaFile.value = "";
          els.selectedFile.textContent = "No file selected";
        }
      });
    });

    els.screenImage.addEventListener("pointerdown", onScreenPointerDown);
    els.screenImage.addEventListener("pointerup", onScreenPointerUp);
    els.screenImage.addEventListener("pointercancel", () => { pointerStart = null; });
    els.screenImage.addEventListener("keydown", onScreenKeyDown);

    document.addEventListener("visibilitychange", () => {
      if (document.hidden) {
        window.clearTimeout(statusTimer);
        window.clearTimeout(screenTimer);
        return;
      }
      scheduleStatus(0);
      scheduleScreen(0);
    });
  }

  function initialize() {
    bindEvents();
    renderStatus();
    void loadDevices();
  }

  initialize();
})();
