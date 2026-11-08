const commandExamples = {
  bootstrap: {
    label: "HOST / BOOTSTRAP",
    caption: "Build the pinned Linux backend, install xtool from your Xcode SDK archive, then check local prerequisites.",
    code: [
      "uv sync --python 3.12",
      "uv run limulator bootstrap deps",
      "uv run limulator bootstrap all",
      "uv run limulator bootstrap xtool",
      "uv run limulator doctor",
    ].join("\n"),
  },
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
let syncCommandProfile = () => {};

async function readJSON(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) throw new Error(`Could not load ${path}: ${response.status}`);
  return response.json();
}

function setText(selector, value) {
  if (value == null) return;
  const node = $(selector);
  if (node) node.textContent = value;
}

function configureProfileTabs(profiles) {
  const buttons = $$(".profile-tab");
  const panel = $("#profile-panel");
  const profileMap = new Map(profiles.map((profile) => [profile.id, profile]));
  profiles.forEach((profile) => {
    $$(`[data-profile-tab-state='${profile.id}']`).forEach((node) => { node.textContent = profile.badge; });
  });

  function drawDiagram(kind, label) {
    const diagram = $("[data-profile-diagram]");
    const diagrams = {
      cover: {
        aria: `Conceptual ${label} cover framebuffer. Its virtual inner display and software postures are experimental; runtime behavior remains unverified.`,
        shapes: '<rect x="113" y="10" width="134" height="196" rx="21"/>',
        caption: "one visible framebuffer · cover profile",
        center: "COVER",
      },
      portrait: {
        aria: `Conceptual ${label} portrait display layout`,
        shapes: '<rect x="135" y="10" width="90" height="195" rx="20"/>',
        caption: "single panel · portrait",
      },
      ipad: {
        aria: `Conceptual ${label} portrait display layout`,
        shapes: '<rect x="116" y="10" width="128" height="195" rx="19"/>',
        caption: "tablet panel · portrait",
      },
      landscape: {
        aria: `Conceptual ${label} landscape display layout`,
        shapes: '<rect x="70" y="38" width="220" height="140" rx="18"/><path class="diagram-speaker" d="M81 92v32"/>',
        caption: "single panel · landscape",
      },
    };
    const config = diagrams[kind] || diagrams.portrait;
    diagram.innerHTML = `<svg viewBox="0 0 360 230" role="img" aria-label="${config.aria}"><g class="diagram-device">${config.shapes}</g>${config.center ? `<text x="180" y="118" text-anchor="middle">${config.center}</text>` : ""}<text x="180" y="221" text-anchor="middle">${config.caption}</text></svg>`;
  }

  function select(id, focus = false) {
    const profile = profileMap.get(id) || profiles[0];
    if (!profile) return;
    buttons.forEach((button, index) => {
      const selected = button.dataset.profile === profile.id;
      button.classList.toggle("is-selected", selected);
      button.setAttribute("aria-selected", String(selected));
      button.tabIndex = selected ? 0 : -1;
      if (selected && focus) button.focus();
      const count = $(".picker-count");
      if (selected && count) count.innerHTML = `${String(index + 1).padStart(2, "0")} <i>/</i> ${String(buttons.length).padStart(2, "0")}`;
    });

    panel.setAttribute("aria-labelledby", `tab-${profile.id}`);
    setText("[data-profile-badge]", profile.badge);
    const badge = $("[data-profile-badge]");
    badge.classList.toggle("badge-research", profile.state === "research");
    badge.classList.toggle("badge-verified", profile.state === "verified");
    setText("[data-profile-source]", profile.source.toUpperCase());
    setText("[data-profile-title]", profile.label);
    setText("[data-profile-form]", profile.form);
    setText("[data-profile-device]", profile.device);
    setText("[data-profile-model]", profile.model);
    setText("[data-profile-os]", profile.guest);
    setText("[data-profile-scale]", profile.scale);
    setText("[data-profile-dimensions]", profile.dimensions);
    setText("[data-profile-inner-display]", profile.innerDisplay);
    $("[data-inner-display-row]").hidden = !profile.innerDisplay;
    setText("[data-profile-postures]", profile.postures);
    setText("[data-profile-note]", profile.note);
    setText("[data-profile-state]", profile.validation || profile.badge);
    setText("[data-profile-evidence]", `Profile ${String(buttons.findIndex((button) => button.dataset.profile === profile.id) + 1).padStart(2, "0")} / ${String(buttons.length).padStart(2, "0")}`);
    const pip = $("[data-profile-pip]");
    pip.classList.toggle("pip-amber", profile.state === "active");
    pip.classList.toggle("pip-lime", profile.state === "verified");
    pip.classList.toggle("pip-gray", profile.state === "research");
    drawDiagram(profile.diagram, profile.label);
    setText("[data-command-research]", `${profile.label}: ${profile.validation || profile.badge}`);
    syncCommandProfile(profile);
  }

  buttons.forEach((button) => button.addEventListener("click", () => select(button.dataset.profile)));
  $$(".profile-tab").forEach((button) => {
    button.addEventListener("keydown", (event) => {
      const current = buttons.indexOf(button);
      let next;
      if (event.key === "ArrowRight" || event.key === "ArrowDown") next = (current + 1) % buttons.length;
      else if (event.key === "ArrowLeft" || event.key === "ArrowUp") next = (current - 1 + buttons.length) % buttons.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = buttons.length - 1;
      if (next !== undefined) {
        event.preventDefault();
        select(buttons[next].dataset.profile, true);
      }
    });
  });

  select($(".profile-tab[aria-selected='true']")?.dataset.profile || profiles[0]?.id);
}

function configureCommands() {
  const buttons = $$(".command-tab");
  const panel = $("#command-panel");
  const code = $("[data-command-code]");
  const copy = $("[data-copy]");
  let selectedCommand = "bootstrap";
  let deviceProfile = { label: "iPhone 16", profileId: "iphone-16", deviceName: "iphone-16" };

  function commandFor(command) {
    if (command === "create") {
      return {
        label: "DEVICE / CREATE GUEST",
        caption: `Create the ${deviceProfile.label} profile after the host prerequisites and backend are ready.`,
        code: `uv run limulator --device ${deviceProfile.deviceName} create --profile ${deviceProfile.profileId}`,
      };
    }
    if (command === "run") {
      return {
        label: "APP / BUILD + INSTALL + LAUNCH",
        caption: `Build CounterLab with xtool, then install and launch it in the ${deviceProfile.label} guest.`,
        code: `uv run limulator --device ${deviceProfile.deviceName} run examples/CounterLab`,
      };
    }
    if (command === "console") {
      return {
        label: `GUEST / INSPECT ${deviceProfile.deviceName.toUpperCase()}`,
        caption: `Start the loopback-only console, capture a genuine guest frame, or exercise native controls on ${deviceProfile.label}.`,
        code: [
          `uv run limulator --device ${deviceProfile.deviceName} serve`,
          "# open http://127.0.0.1:18765",
          `uv run limulator --device ${deviceProfile.deviceName} screenshot artifacts/frame.png`,
          `uv run limulator --device ${deviceProfile.deviceName} tap 300 500`,
        ].join("\n"),
      };
    }
    return commandExamples[command] || commandExamples.bootstrap;
  }

  function select(command, focus = false) {
    const config = commandFor(command);
    selectedCommand = command;
    buttons.forEach((button) => {
      const selected = button.dataset.command === command;
      button.classList.toggle("is-selected", selected);
      button.setAttribute("aria-selected", String(selected));
      button.tabIndex = selected ? 0 : -1;
      if (selected && focus) button.focus();
    });
    const activeButton = buttons.find((button) => button.dataset.command === command);
    panel.setAttribute("aria-labelledby", activeButton?.id || "command-tab-bootstrap");
    setText("[data-command-label]", config.label);
    code.textContent = config.code;
    setText("[data-command-caption]", config.caption);
    setText("[data-copy-status]", "");
    setText("[data-copy] span", "Copy");
  }

  buttons.forEach((button) => button.addEventListener("click", () => select(button.dataset.command)));
  buttons.forEach((button) => {
    button.addEventListener("keydown", (event) => {
      const current = buttons.indexOf(button);
      let next;
      if (event.key === "ArrowRight" || event.key === "ArrowDown") next = (current + 1) % buttons.length;
      else if (event.key === "ArrowLeft" || event.key === "ArrowUp") next = (current - 1 + buttons.length) % buttons.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = buttons.length - 1;
      if (next !== undefined) {
        event.preventDefault();
        select(buttons[next].dataset.command, true);
      }
    });
  });

  async function copyCurrent() {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(commandFor(selectedCommand).code);
      } else {
        const textarea = document.createElement("textarea");
        textarea.value = commandFor(selectedCommand).code;
        textarea.setAttribute("readonly", "");
        textarea.style.position = "fixed";
        textarea.style.opacity = "0";
        document.body.append(textarea);
        textarea.select();
        const copied = document.execCommand("copy");
        textarea.remove();
        if (!copied) throw new Error("Clipboard copy is unavailable");
      }
      setText("[data-copy] span", "Copied");
      setText("[data-copy-status]", "Commands copied to clipboard.");
      window.setTimeout(() => setText("[data-copy] span", "Copy"), 1600);
    } catch {
      setText("[data-copy] span", "Select text");
      setText("[data-copy-status]", "Clipboard access is unavailable. Select and copy the command text.");
    }
  }

  copy.addEventListener("click", copyCurrent);
  syncCommandProfile = (profile) => {
    deviceProfile = profile;
    const label = $("[data-command-profile]");
    if (label) label.textContent = `${profile.deviceName.toUpperCase()} / ${profile.profileId.toUpperCase()}`;
    if (["create", "run", "console"].includes(selectedCommand)) select(selectedCommand);
  };
  select("bootstrap");
}

function safeCapturePath(path) {
  return typeof path === "string" && /^assets\/native\/[A-Za-z0-9._/-]+\.png$/i.test(path) && !path.split("/").includes("..");
}

function renderCaptures(captures = []) {
  const grid = $("[data-capture-grid]");
  const count = $("[data-capture-count]");
  const validCaptures = captures.filter((capture) => safeCapturePath(capture.file));
  count.textContent = `${validCaptures.length} ${validCaptures.length === 1 ? "capture" : "captures"}`;
  const emptyState = grid.querySelector(".capture-empty")?.cloneNode(true);
  grid.replaceChildren();

  if (validCaptures.length === 0) {
    if (emptyState) grid.append(emptyState);
    return;
  }

  validCaptures.forEach((capture) => {
    const figure = document.createElement("figure");
    figure.className = "capture-card";
    const imageWrap = document.createElement("a");
    imageWrap.className = "capture-image-wrap";
    imageWrap.href = new URL(capture.file, document.baseURI).href;
    imageWrap.title = "Open original PNG at full resolution";
    const image = document.createElement("img");
    image.src = imageWrap.href;
    image.alt = capture.alt || `${capture.title || "Native guest frame"}${capture.posture ? `, ${capture.posture} posture` : ""}`;
    image.loading = "lazy";
    image.decoding = "async";
    imageWrap.append(image);
    const caption = document.createElement("figcaption");
    const title = document.createElement("strong");
    title.textContent = capture.title || "Native guest capture";
    const detail = document.createElement("span");
    detail.textContent = capture.context || "Captured from the iOS guest framebuffer.";
    const metadata = document.createElement("div");
    metadata.className = "capture-meta";
    const device = document.createElement("span");
    device.textContent = [capture.device, capture.os].filter(Boolean).join(" · ") || "iOS guest";
    const posture = document.createElement("span");
    posture.textContent = capture.posture || "";
    metadata.append(device, posture);
    caption.append(title, detail, metadata);
    figure.append(imageWrap, caption);
    grid.append(figure);
  });
}

function renderHeroCapture(captures = []) {
  const candidates = captures.filter((capture) => safeCapturePath(capture.file));
  if (!candidates.length) return;

  const visual = $("[data-hero-visual]");
  const schematic = $("[data-hero-schematic]");
  const figure = $("[data-hero-capture]");
  const image = $("[data-hero-capture-image]");
  const caption = $("[data-hero-visual-caption]");
  const originalLabel = visual.getAttribute("aria-label");
  let candidateIndex = 0;

  const showCapture = (capture) => {
    const profile = [capture.device, capture.os, capture.posture].filter(Boolean).join(" · ");
    const title = capture.title || "Native guest frame";
    const description = capture.alt || [title, profile].filter(Boolean).join(" · ");
    const profileNode = $("[data-hero-capture-profile]");
    const contextNode = $("[data-hero-capture-context]");

    image.alt = description;
    $("[data-hero-capture-title]").textContent = title;
    profileNode.textContent = profile;
    profileNode.hidden = !profile;
    contextNode.textContent = capture.context || "";
    contextNode.hidden = !capture.context;
    schematic.toggleAttribute("hidden", true);
    figure.hidden = false;
    caption.hidden = true;
    setText("[data-hero-visual-kind]", "GUEST FRAME / 01");
    setText("[data-hero-visual-mode]", "NATIVE PNG");
    visual.setAttribute("aria-label", `Native guest capture: ${description}`);
  };

  const tryNextCapture = () => {
    const capture = candidates[candidateIndex++];
    if (!capture) {
      image.onload = null;
      image.onerror = null;
      image.removeAttribute("src");
      image.alt = "";
      figure.hidden = true;
      schematic.toggleAttribute("hidden", false);
      caption.hidden = false;
      setText("[data-hero-visual-kind]", "DEVICE PROFILE / 01");
      setText("[data-hero-visual-mode]", "SCHEMATIC");
      visual.setAttribute("aria-label", originalLabel);
      return;
    }

    image.onload = () => {
      if (image.naturalWidth > 0 && image.naturalHeight > 0) showCapture(capture);
      else tryNextCapture();
    };
    image.onerror = tryNextCapture;
    image.loading = "eager";
    image.decoding = "async";
    image.src = new URL(capture.file, document.baseURI).href;
  };

  tryNextCapture();
}

function renderBenchmark(benchmark) {
  if (!benchmark) return;
  setText("[data-benchmark-title]", benchmark.title);
  setText("[data-benchmark-description]", benchmark.description);
  setText("[data-benchmark-runs]", `${benchmark.runs} interleaved runs`);
  setText("[data-benchmark-scope]", benchmark.scope);
  const results = $("[data-benchmark-results]");
  results.replaceChildren();
  (benchmark.results || []).forEach((result, index) => {
    if (index === 1) {
      const divider = document.createElement("div");
      divider.className = "benchmark-divider";
      divider.setAttribute("aria-hidden", "true");
      divider.textContent = "→";
      results.append(divider);
    }
    const cell = document.createElement("div");
    cell.className = `benchmark-result${index === 1 ? " benchmark-result-accent" : ""}`;
    const label = document.createElement("span");
    label.textContent = result.label;
    const value = document.createElement("strong");
    const valueText = (result.display || `${result.seconds} s`).replace(/\s+s$/, "");
    value.append(document.createTextNode(valueText));
    const unit = document.createElement("small");
    unit.textContent = "s";
    value.append(" ", unit);
    const method = document.createElement("code");
    method.textContent = result.method || "";
    cell.append(label, value, method);
    results.append(cell);
  });
}

function renderApplicationMeasurements(measurements = []) {
  const note = $(".app-measurement-note");
  if (!measurements.length) return;
  const heading = document.createElement("strong");
  heading.id = "app-measurements-heading";
  heading.textContent = `${measurements.length} app measurement${measurements.length === 1 ? "" : "s"} recorded`;
  const list = document.createElement("dl");
  list.className = "application-measurements";
  measurements.forEach((item) => {
    const row = document.createElement("div");
    row.className = "application-measurement-row";
    const label = document.createElement("dt");
    label.textContent = item.label || "Unlabeled measurement";
    const detail = document.createElement("dd");
    if (item.value != null || item.unit) {
      const value = document.createElement("strong");
      value.className = "application-measurement-value";
      value.textContent = [item.value, item.unit].filter((part) => part != null && part !== "").join(" ");
      detail.append(value);
    }
    if (item.context) {
      const context = document.createElement("p");
      context.className = "application-measurement-context";
      context.textContent = item.context;
      detail.append(context);
    }
    row.append(label, detail);
    list.append(row);
  });
  const content = note.querySelector("div");
  content.replaceChildren(heading, list);
  note.classList.add("has-measurements");
  note.setAttribute("role", "group");
  note.setAttribute("aria-labelledby", heading.id);
  note.querySelector(".measurement-icon").hidden = true;
}

function applyEvidence(evidence) {
  setText("[data-release-label]", evidence.release?.label);
  setText("[data-footer-status]", evidence.release?.label);
  setText(".hero-release > span:last-child", evidence.release?.detail);
  setText("[data-hero-guest-state]", evidence.guest?.value);
  setText("[data-hero-guest-device]", evidence.guest?.device);
  setText("[data-hero-guest-os]", evidence.guest?.os);
  setText("[data-guest-device]", evidence.guest?.device ? `${evidence.guest.device} guest` : null);
  setText("[data-guest-os]", evidence.guest?.os);
  for (const key of ["guest", "sample", "runtime"]) {
    const item = evidence[key];
    if (!item) continue;
    const value = document.querySelector(`[data-status='${key}'] [data-status-value]`);
    if (value) value.textContent = item.value;
    setText(`[data-proof-state='${key}']`, item.proofValue || item.value);
    setText(`[data-proof-label='${key}']`, item.proofLabel);
  }
  if (evidence.guest?.detail) {
    setText(".proof-progress > p:not(.proof-label)", evidence.guest.detail);
  }
  if (evidence.sample?.detail) {
    setText(".proof-verified > p:not(.proof-label)", evidence.sample.detail);
  }
  if (evidence.runtime?.detail) {
    setText(".proof-next > p:not(.proof-label)", evidence.runtime.detail);
  }
  setText("[data-workflow-step='sample']", evidence.sample?.workflowValue || evidence.sample?.value);
  setText("[data-workflow-step='runtime']", evidence.runtime?.workflowValue || evidence.runtime?.value);
  const captureCount = evidence.captures?.length || 0;
  setText("[data-workflow-step='capture']", evidence.runtime?.captureValue || (captureCount ? `${captureCount} guest captures added` : "Not yet captured"));
  setText("[data-workflow-note]", evidence.workflowNote);
  setText("[data-faq-runtime]", evidence.runtime?.faq);
  setText("[data-faq-release]", evidence.faq?.release);
  renderHeroCapture(evidence.captures || []);
  renderCaptures(evidence.captures || []);
  renderBenchmark(evidence.microbenchmark);
  renderApplicationMeasurements(evidence.applicationMeasurements || []);
}

function configureMenu() {
  const toggle = $(".menu-toggle");
  const nav = $("#primary-nav");
  function close() {
    toggle.setAttribute("aria-expanded", "false");
    toggle.querySelector(".sr-only").textContent = "Open navigation";
    nav.classList.remove("is-open");
  }
  toggle.addEventListener("click", () => {
    const open = toggle.getAttribute("aria-expanded") !== "true";
    toggle.setAttribute("aria-expanded", String(open));
    toggle.querySelector(".sr-only").textContent = open ? "Close navigation" : "Open navigation";
    nav.classList.toggle("is-open", open);
  });
  nav.addEventListener("click", (event) => {
    if (event.target.closest("a")) close();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && toggle.getAttribute("aria-expanded") === "true") {
      close();
      toggle.focus();
    }
  });
}

configureMenu();
configureCommands();

Promise.all([readJSON("./data/profiles.json"), readJSON("./data/evidence.json")])
  .then(([profileData, evidence]) => {
    configureProfileTabs(profileData.profiles || []);
    applyEvidence(evidence);
  })
  .catch((error) => {
    console.error(error);
    const grid = $("[data-capture-grid]");
    grid.textContent = "Project evidence data could not be loaded. Serve the built site over HTTP to preview it.";
  });
