const state = {
  config: null,
  selectedDevice: null,
  dirty: false,
  statusTimer: null,
  baseTitle: document.title,
};

const elements = {};

const DEFAULT_DEVICE_SETTINGS = {
  "scpi-tcp": { host: "127.0.0.1", port: 5025 },
  "scpi-serial": { port: "COM1", baudrate: 9600, parity: "N", stopbits: 1 },
  "modbus-tcp": { host: "127.0.0.1", port: 502 },
  "modbus-rtu": { port: "COM2", baudrate: 19200, parity: "E", stopbits: 1, unit_id: 1 },
  "modbus-ascii": { port: "COM3", baudrate: 9600, parity: "E", stopbits: 1, unit_id: 1 },
  "generic-regex": { pattern: "", response: "" },
  loopback: {},
  usbtmc: { vendor_id: "0x0000", product_id: "0x0000" },
};

// Keys that should never be user-editable in the device parameters table
const RESERVED_DEVICE_KEYS = new Set(["type", "mappings"]);

const MAPPING_ACTIONS = [
  "read_holding_registers",
  "read_input_registers",
  "read_coils",
  "read_discrete_inputs",
  "write_single_register",
  "write_holding_registers",
  "write_single_coil",
  "write_multiple_coils",
];

window.addEventListener("DOMContentLoaded", () => {
  cacheElements();
  bindStaticHandlers();
  refreshConfig();
});

function cacheElements() {
  elements.serverHost = document.getElementById("server-host");
  elements.serverPort = document.getElementById("server-port");
  elements.serverPortmapper = document.getElementById("server-portmapper");
  elements.guiEnabled = document.getElementById("gui-enabled");
  elements.guiHost = document.getElementById("gui-host");
  elements.guiPort = document.getElementById("gui-port");
  elements.deviceList = document.getElementById("device-list");
  elements.deviceEditor = document.getElementById("device-editor");
  elements.deviceName = document.getElementById("device-name");
  elements.deviceType = document.getElementById("device-type");
  elements.deviceParams = document.getElementById("device-params");
  elements.btnAddParam = document.getElementById("btn-add-param");
  elements.btnDeleteDevice = document.getElementById("btn-delete-device");
  elements.btnAddDevice = document.getElementById("btn-add-device");
  elements.btnSave = document.getElementById("btn-save");
  elements.btnReload = document.getElementById("btn-reload");
  elements.status = document.getElementById("status");
  elements.mappingDeviceIndicator = document.getElementById("mapping-device-indicator");
  elements.mappingEditor = document.getElementById("mapping-editor");
  elements.mappingRows = document.getElementById("mapping-rows");
  elements.btnAddMapping = document.getElementById("btn-add-mapping");
  elements.paramRowTemplate = document.getElementById("param-row-template");
  elements.mappingRowTemplate = document.getElementById("mapping-row-template");
  elements.mappingParamTemplate = document.getElementById("mapping-param-template");
}

function bindStaticHandlers() {
  elements.btnSave.addEventListener("click", handleSave);
  elements.btnReload.addEventListener("click", handleReload);
  elements.btnAddDevice.addEventListener("click", handleAddDevice);
  elements.btnAddParam.addEventListener("click", handleAddDeviceParam);
  elements.btnDeleteDevice.addEventListener("click", handleDeleteDevice);
  elements.deviceParams.addEventListener("input", handleDeviceParamsChanged, true);
  elements.deviceParams.addEventListener("change", handleDeviceParamsChanged, true);
  elements.deviceParams.addEventListener("click", handleDeviceParamsClick);
  elements.deviceName.addEventListener("blur", handleDeviceRename);
  elements.deviceName.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      elements.deviceName.blur();
    }
  });
  elements.deviceType.addEventListener("change", handleDeviceTypeChange);

  elements.btnAddMapping.addEventListener("click", handleAddMappingRule);
  elements.mappingRows.addEventListener("input", handleMappingChanged, true);
  elements.mappingRows.addEventListener("change", handleMappingChanged, true);
  elements.mappingRows.addEventListener("click", handleMappingClick);

  elements.serverHost.addEventListener("input", () => updateServerField("host", elements.serverHost.value));
  elements.serverPort.addEventListener("input", () =>
    updateServerField("port", coerceNumber(elements.serverPort.value))
  );
  elements.serverPortmapper.addEventListener("change", () =>
    updateServerField("portmapper_enabled", elements.serverPortmapper.value === "true")
  );
  elements.guiEnabled.addEventListener("change", () => {
    updateGuiField("enabled", elements.guiEnabled.value === "true");
    updateGuiEnabledState();
  });
  elements.guiHost.addEventListener("input", () => updateGuiField("host", elements.guiHost.value));
  elements.guiPort.addEventListener("input", () =>
    updateGuiField("port", coerceNumber(elements.guiPort.value))
  );
  // Admin handlers
  const refreshBtn = document.getElementById("btn-refresh-locks");
  if (refreshBtn) {
    refreshBtn.addEventListener("click", fetchLocks);
  }
}

async function fetchLocks() {
  try {
    const resp = await fetch("/api/admin/locks");
    if (!resp.ok) throw new Error(`Failed to fetch locks (${resp.status})`);
    const data = await resp.json();
    const owners = data.owners || {};
    const tbody = document.getElementById("admin-locks-rows");
    tbody.innerHTML = "";
    const entries = Object.entries(owners).sort((a, b) => a[0].localeCompare(b[0]));
    if (!entries.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 2;
      td.className = "muted";
      td.textContent = "No locks held";
      tr.appendChild(td);
      tbody.appendChild(tr);
      return;
    }
    for (const [device, owner] of entries) {
      const tr = document.createElement("tr");
      const tdDev = document.createElement("td");
      tdDev.textContent = device;
      const tdOwner = document.createElement("td");
      tdOwner.textContent = owner === null ? "(none)" : String(owner);
      tr.appendChild(tdDev);
      tr.appendChild(tdOwner);
      tbody.appendChild(tr);
    }
  } catch (err) {
    console.error(err);
    setStatus(err.message || "Failed to fetch locks", "error");
  }
}

async function refreshConfig(showStatus = true) {
  try {
    const response = await fetch("/api/config", { headers: { Accept: "application/json" } });
    if (!response.ok) {
      throw new Error(`Failed to load configuration (${response.status})`);
    }
    const data = await response.json();
    state.config = normaliseConfig(data);
    ensureSelections();
    renderAll();
    markDirty(false);
    if (showStatus) {
      setStatus("Configuration loaded", "success");
    }
    return true;
  } catch (error) {
    console.error(error);
    setStatus(error.message || "Failed to load configuration", "error");
    return false;
  }
}

function normaliseConfig(raw) {
  const config = {
    server: raw.server || {},
    devices: raw.devices ? { ...raw.devices } : {},
    mappings: raw.mappings ? { ...raw.mappings } : {},
  };

  if (!config.server.gui) {
    config.server.gui = { enabled: true, host: "127.0.0.1", port: 0 };
  }

  // Migrate any embedded device-level mappings into the top-level mappings section
  for (const [name, def] of Object.entries(config.devices)) {
    if (!def || typeof def !== "object") continue;
    if (!Object.prototype.hasOwnProperty.call(def, "mappings")) continue;
    let embedded = def.mappings;
    delete def.mappings; // remove from device settings

    // If embedded is a JSON string, attempt to parse it
    if (typeof embedded === "string") {
      try {
        const parsed = JSON.parse(embedded);
        embedded = parsed;
      } catch (_err) {
        // leave embedded as-is; it won't be used if not an array
      }
    }
    if (Array.isArray(embedded)) {
      // Only adopt if there isn't already top-level mappings for this device
      if (!Array.isArray(config.mappings[name]) || config.mappings[name].length === 0) {
        config.mappings[name] = embedded;
      }
    }
  }

  return config;
}

function ensureSelections() {
  const deviceNames = Object.keys(state.config.devices);
  if (!deviceNames.includes(state.selectedDevice)) {
    state.selectedDevice = deviceNames[0] || null;
  }
}

function renderAll() {
  renderServerSettings();
  renderDeviceList();
  renderDeviceEditor();
  renderMappingDeviceOptions();
  renderMappingRows();
  updateSaveButtonState();
  updateGuiEnabledState();
}

function renderServerSettings() {
  const server = state.config.server || {};
  elements.serverHost.value = server.host ?? "";
  elements.serverPort.value = server.port ?? "";
  elements.serverPortmapper.value = server.portmapper_enabled ? "true" : "false";

  const gui = server.gui || {};
  elements.guiEnabled.value = gui.enabled === false ? "false" : "true";
  elements.guiHost.value = gui.host ?? "";
  elements.guiPort.value = gui.port ?? "";
}

function renderDeviceList() {
  elements.deviceList.innerHTML = "";
  const names = Object.keys(state.config.devices).sort();
  if (!names.length) {
    const placeholder = document.createElement("li");
    placeholder.textContent = "No devices configured";
    placeholder.className = "muted";
    elements.deviceList.appendChild(placeholder);
    return;
  }

  for (const name of names) {
    const li = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = name;
    if (name === state.selectedDevice) {
      button.classList.add("active");
    }
    button.addEventListener("click", () => {
      state.selectedDevice = name;
      renderDeviceList();
      renderDeviceEditor();
      renderMappingDeviceOptions();
      renderMappingRows();
    });
    li.appendChild(button);
    elements.deviceList.appendChild(li);
  }
}

function renderDeviceEditor() {
  if (!state.selectedDevice || !state.config.devices[state.selectedDevice]) {
    elements.deviceEditor.hidden = true;
    return;
  }

  const definition = state.config.devices[state.selectedDevice];
  elements.deviceEditor.hidden = false;
  elements.deviceName.value = state.selectedDevice;
  elements.deviceType.value = definition.type || "scpi-tcp";
  renderDeviceParameters(definition);
}

function renderDeviceParameters(definition) {
  elements.deviceParams.innerHTML = "";
  const settings = definition ? { ...definition } : {};
  delete settings.type;
  delete settings.mappings; // mappings are managed in dedicated section
  // Do not render reserved/derived keys inside device settings
  delete settings.mappings;

  const entries = Object.entries(settings);
  if (!entries.length) {
    appendParamRow("", "");
    return;
  }

  for (const [key, value] of entries) {
    appendParamRow(key, stringifyValue(value));
  }
}

function isDeviceMappable(name) {
  if (!name) return false;
  const def = state.config.devices[name];
  if (!def) return false;
  const t = (def.type || "").toLowerCase();
  return t.startsWith("modbus") || t === "generic-regex";
}

function renderMappingDeviceOptions() {
  const name = state.selectedDevice;
  const mappable = isDeviceMappable(name);
  if (elements.mappingDeviceIndicator) {
    elements.mappingDeviceIndicator.textContent =
      name ? (mappable ? `Device: ${name}` : `Device: ${name} (no mappings)`) : "";
  }
  elements.btnAddMapping.disabled = !mappable;
  elements.mappingEditor.hidden = !mappable;
}

function renderMappingRows() {
  elements.mappingRows.innerHTML = "";
  const deviceName = state.selectedDevice;
  if (!deviceName || !isDeviceMappable(deviceName)) {
    elements.mappingEditor.hidden = true;
    return;
  }

  const rules = state.config.mappings[deviceName] || [];
  if (!rules.length) {
    appendMappingRow({ pattern: "", action: MAPPING_ACTIONS[0], params: {} });
    return;
  }

  rules.forEach((rule) => appendMappingRow(rule));
}

function appendParamRow(key, value) {
  const template = elements.paramRowTemplate.content.cloneNode(true);
  const row = template.querySelector("tr");
  const keyInput = row.querySelector(".param-key");
  const valueInput = row.querySelector(".param-value");
  keyInput.value = key;
  valueInput.value = value;
  elements.deviceParams.appendChild(row);
}

function appendMappingRow(rule) {
  const template = elements.mappingRowTemplate.content.cloneNode(true);
  const row = template.querySelector("tr");
  const patternInput = row.querySelector(".mapping-pattern");
  const actionSelect = row.querySelector(".mapping-action");
  patternInput.value = rule.pattern ?? "";
  // Choose action control type based on device type: select for MODBUS, free-text for regex
  const deviceName = state.selectedDevice;
  const deviceType = deviceName ? (state.config.devices[deviceName]?.type || "").toLowerCase() : "";
  if (deviceType.startsWith("modbus")) {
    // Ensure it's a SELECT and populate with known actions
    if (actionSelect.tagName !== "SELECT") {
      const replacement = document.createElement("select");
      replacement.className = "mapping-action";
      actionSelect.replaceWith(replacement);
    }
    populateActionSelect(row.querySelector(".mapping-action"), rule.action);
  } else {
    // Replace the action cell with regex-specific editors
    const actionCell = actionSelect.parentElement;
    const editor = document.createElement("div");
    editor.className = "regex-editor";
    editor.appendChild(makeLabeledInput("Request", "regex-request-format", rule.request_format ?? ""));
    editor.appendChild(makeLabeledInput("Resp Regex", "regex-response-regex", rule.response_regex ?? ""));
    editor.appendChild(makeLabeledInput("Resp Format", "regex-response-format", rule.response_format ?? ""));
    editor.appendChild(makeLabeledInput("Static Response", "regex-response-static", rule.response ?? ""));
    actionCell.replaceChildren(editor);
  }
  const paramsContainer = row.querySelector(".param-list");
  if (deviceType.startsWith("modbus")) {
    const params = rule.params || {};
    const entries = Object.entries(params);
    if (!entries.length) {
      paramsContainer.appendChild(createMappingParamChip("", ""));
    } else {
      for (const [key, value] of entries) {
        paramsContainer.appendChild(createMappingParamChip(key, stringifyValue(value)));
      }
    }
  } else {
    const knownRegexFields = new Set([
      "pattern",
      "request_format",
      "response_regex",
      "response_format",
      "response",
      "payload_width",
      "expects_response",
      "scale",
      "terminator",
      "response_scale",
      "params",
      "action",
    ]);
    // Structured options
    const options = document.createElement("div");
    options.className = "regex-options";
    options.appendChild(makeLabeledInput("Payload Width", "regex-opt", rule.payload_width ?? "", "number", "payload_width"));
    options.appendChild(makeLabeledCheckbox("Expects Response", "regex-opt", !!rule.expects_response, "expects_response"));
    options.appendChild(makeLabeledInput("Scale", "regex-opt", rule.scale ?? "", "number", "scale"));
    options.appendChild(makeLabeledInput("Terminator", "regex-opt", rule.terminator ?? "", "text", "terminator"));
    options.appendChild(makeLabeledInput("Resp Scale", "regex-opt", rule.response_scale ?? "", "number", "response_scale"));
    paramsContainer.before(options);

    // Extra, unknown fields as chips
    const extras = Object.entries(rule).filter(([k]) => !knownRegexFields.has(k));
    if (!extras.length) {
      paramsContainer.appendChild(createMappingParamChip("", ""));
    } else {
      for (const [key, value] of extras) {
        paramsContainer.appendChild(createMappingParamChip(key, stringifyValue(value)));
      }
    }
  }
  elements.mappingRows.appendChild(row);
}

function populateActionSelect(select, selected) {
  select.innerHTML = "";
  for (const action of MAPPING_ACTIONS) {
    const option = document.createElement("option");
    option.value = action;
    option.textContent = action;
    if (action === selected) {
      option.selected = true;
    }
    select.appendChild(option);
  }
}

function createMappingParamChip(key, value) {
  const template = elements.mappingParamTemplate.content.cloneNode(true);
  const chip = template.querySelector(".param-chip");
  const keyInput = chip.querySelector(".chip-key");
  const valueInput = chip.querySelector(".chip-value");
  keyInput.value = key;
  valueInput.value = value;
  return chip;
}

function makeLabeledInput(labelText, cls, value, type = "text", dataKey = null) {
  const wrap = document.createElement("label");
  wrap.style.display = "block";
  wrap.style.marginBottom = "0.35rem";
  wrap.textContent = labelText;
  const input = document.createElement("input");
  input.type = type;
  input.className = cls;
  if (dataKey) input.dataset.key = dataKey;
  input.value = value == null ? "" : String(value);
  input.style.display = "block";
  input.style.width = "100%";
  input.style.marginTop = "0.2rem";
  wrap.appendChild(input);
  return wrap;
}

function makeLabeledCheckbox(labelText, cls, checked, dataKey) {
  const wrap = document.createElement("label");
  wrap.style.display = "inline-flex";
  wrap.style.alignItems = "center";
  wrap.style.gap = "0.4rem";
  wrap.style.marginRight = "0.6rem";
  const input = document.createElement("input");
  input.type = "checkbox";
  input.className = cls;
  input.dataset.key = dataKey;
  input.checked = !!checked;
  const span = document.createElement("span");
  span.textContent = labelText;
  wrap.appendChild(input);
  wrap.appendChild(span);
  return wrap;
}

function handleAddDevice() {
  ensureConfigLoaded();
  const existingNames = new Set(Object.keys(state.config.devices));
  let index = existingNames.size + 1;
  let candidate = `device_${index}`;
  while (existingNames.has(candidate)) {
    index += 1;
    candidate = `device_${index}`;
  }
  const type = "scpi-tcp";
  state.config.devices[candidate] = { type, ...DEFAULT_DEVICE_SETTINGS[type] };
  state.selectedDevice = candidate;
  markDirty(true);
  renderDeviceList();
  renderDeviceEditor();
  renderMappingDeviceOptions();
  renderMappingRows();
  setStatus(`Added device ${candidate}`, "success");
}

function handleDeleteDevice() {
  ensureConfigLoaded();
  if (!state.selectedDevice) {
    return;
  }
  const name = state.selectedDevice;
  if (!window.confirm(`Delete device "${name}"?`)) {
    return;
  }
  delete state.config.devices[name];
  if (state.config.mappings[name]) {
    delete state.config.mappings[name];
  }
  state.selectedDevice = null;
  markDirty(true);
  ensureSelections();
  renderAll();
  setStatus(`Deleted device ${name}`, "success");
}

function handleAddDeviceParam() {
  if (!state.selectedDevice || !state.config.devices[state.selectedDevice]) {
    return;
  }
  appendParamRow("", "");
  markDirty(true);
}

function handleDeviceParamsChanged() {
  syncDeviceParamsFromDom();
}

function handleDeviceParamsClick(event) {
  const button = event.target.closest(".icon-button");
  if (!button) {
    return;
  }
  const row = button.closest("tr");
  if (!row) {
    return;
  }
  row.remove();
  syncDeviceParamsFromDom();
}

function syncDeviceParamsFromDom() {
  const deviceName = state.selectedDevice;
  if (!deviceName) {
    return;
  }
  const definition = state.config.devices[deviceName];
  if (!definition) {
    return;
  }

  const settings = {};
  const rows = elements.deviceParams.querySelectorAll("tr");
  rows.forEach((row) => {
    const keyInput = row.querySelector(".param-key");
    const valueInput = row.querySelector(".param-value");
    if (!keyInput || !valueInput) {
      return;
    }
    const key = keyInput.value.trim();
    if (!key) {
      return;
    }
    if (RESERVED_DEVICE_KEYS.has(key)) {
      // ignore reserved keys like "mappings"
      return;
    }
    settings[key] = coerceValue(valueInput.value);
  });

  const type = definition.type || "scpi-tcp";
  state.config.devices[deviceName] = { type, ...settings };
  markDirty(true);
}

function handleDeviceRename() {
  const oldName = state.selectedDevice;
  if (!oldName) {
    return;
  }
  const proposed = elements.deviceName.value.trim();
  if (!proposed) {
    elements.deviceName.value = oldName;
    setStatus("Device name cannot be empty", "error");
    return;
  }
  if (proposed === oldName) {
    return;
  }
  if (state.config.devices[proposed]) {
    elements.deviceName.value = oldName;
    setStatus(`Device ${proposed} already exists`, "error");
    return;
  }

  const definition = state.config.devices[oldName];
  delete state.config.devices[oldName];
  state.config.devices[proposed] = definition;
  if (state.config.mappings[oldName]) {
    state.config.mappings[proposed] = state.config.mappings[oldName];
    delete state.config.mappings[oldName];
  }
  state.selectedDevice = proposed;
  markDirty(true);
  renderDeviceList();
  renderMappingDeviceOptions();
  renderMappingRows();
  setStatus(`Renamed device to ${proposed}`, "success");
}

function handleDeviceTypeChange() {
  const deviceName = state.selectedDevice;
  if (!deviceName) {
    return;
  }
  const newType = elements.deviceType.value;
  const definition = state.config.devices[deviceName] || {};
  const existingSettings = { ...definition };
  delete existingSettings.type;
  const defaults = DEFAULT_DEVICE_SETTINGS[newType] || {};
  state.config.devices[deviceName] = { type: newType, ...defaults, ...existingSettings };
  markDirty(true);
  renderDeviceParameters(state.config.devices[deviceName]);
  renderMappingDeviceOptions();
  renderMappingRows();
}

function handleAddMappingRule() {
  const deviceName = state.selectedDevice;
  if (!deviceName) {
    return;
  }
  state.config.mappings[deviceName] = state.config.mappings[deviceName] || [];
  const newRule = { pattern: "", action: MAPPING_ACTIONS[0], params: {} };
  state.config.mappings[deviceName].push(newRule);
  appendMappingRow(newRule);
  markDirty(true);
}

function handleMappingChanged() {
  syncMappingsFromDom();
}

function handleMappingClick(event) {
  const removeRuleButton = event.target.closest(".remove-mapping");
  if (removeRuleButton) {
    const row = removeRuleButton.closest("tr");
    row?.remove();
    syncMappingsFromDom();
    return;
  }

  const addParamButton = event.target.closest(".add-param");
  if (addParamButton) {
    const paramsContainer = addParamButton.previousElementSibling;
    if (paramsContainer) {
      paramsContainer.appendChild(createMappingParamChip("", ""));
      markDirty(true);
    }
    return;
  }

  const removeParamButton = event.target.closest(".param-chip .icon-button");
  if (removeParamButton) {
    const chip = removeParamButton.closest(".param-chip");
    chip?.remove();
    syncMappingsFromDom();
  }
}

function syncMappingsFromDom() {
  const deviceName = state.selectedDevice;
  if (!deviceName) {
    return;
  }
  const rows = elements.mappingRows.querySelectorAll("tr");
  const collected = [];
  rows.forEach((row) => {
    const patternInput = row.querySelector(".mapping-pattern");
    const paramsContainer = row.querySelector(".param-list");
    if (!patternInput || !paramsContainer) {
      return;
    }
    const deviceType = (state.config.devices[deviceName]?.type || "").toLowerCase();
    if (deviceType.startsWith("modbus")) {
      const actionSelect = row.querySelector(".mapping-action");
      if (!actionSelect) return;
      const params = {};
      paramsContainer.querySelectorAll(".param-chip").forEach((chip) => {
        const keyInput = chip.querySelector(".chip-key");
        const valueInput = chip.querySelector(".chip-value");
        if (!keyInput || !valueInput) {
          return;
        }
        const key = keyInput.value.trim();
        if (!key) {
          return;
        }
        params[key] = coerceValue(valueInput.value);
      });
      collected.push({ pattern: patternInput.value, action: actionSelect.value, params });
    } else {
      const req = row.querySelector(".regex-request-format");
      const respRegex = row.querySelector(".regex-response-regex");
      const respFmt = row.querySelector(".regex-response-format");
      const respStatic = row.querySelector(".regex-response-static");
      const rule = { pattern: patternInput.value };
      if (req) rule["request_format"] = req.value;
      if (respRegex) rule["response_regex"] = respRegex.value;
      if (respFmt) rule["response_format"] = respFmt.value;
      if (respStatic && respStatic.value !== "") rule["response"] = respStatic.value;
      row.querySelectorAll(".regex-options .regex-opt").forEach((el) => {
        const key = el.dataset.key;
        if (!key) return;
        let val;
        if (el.type === "checkbox") {
          val = el.checked;
        } else {
          val = coerceValue(el.value);
        }
        if (el.value !== "" || el.type === "checkbox") {
          rule[key] = val;
        }
      });
      paramsContainer.querySelectorAll(".param-chip").forEach((chip) => {
        const keyInput = chip.querySelector(".chip-key");
        const valueInput = chip.querySelector(".chip-value");
        if (!keyInput || !valueInput) return;
        const key = keyInput.value.trim();
        if (!key) return;
        if (!(key in rule)) {
          rule[key] = coerceValue(valueInput.value);
        }
      });
      collected.push(rule);
    }
  });
  state.config.mappings[deviceName] = collected;
  markDirty(true);
}

async function handleSave() {
  if (!state.config) {
    return;
  }
  const validationError = validateConfig();
  if (validationError) {
    setStatus(validationError, "error");
    return;
  }
  try {
    setStatus("Saving...");
    elements.btnSave.disabled = true;
    const response = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state.config),
    });
    if (!response.ok) {
      const text = await response.text();
      throw new Error(text || `Save failed (${response.status})`);
    }
    const refreshed = await refreshConfig(false);
    if (!refreshed) {
      throw new Error("Configuration reload failed after save");
    }
    setStatus("Configuration saved", "success");
  } catch (error) {
    console.error(error);
    setStatus(error.message || "Failed to save configuration", "error");
    updateSaveButtonState();
  }
}

async function handleReload() {
  try {
    setStatus("Reloading...");
    const response = await fetch("/api/reload", { method: "POST" });
    if (!response.ok) {
      const text = await response.text();
      throw new Error(text || `Reload failed (${response.status})`);
    }
    setStatus("Reload requested", "success");
  } catch (error) {
    console.error(error);
    setStatus(error.message || "Failed to reload", "error");
  }
}

function updateServerField(key, value) {
  ensureConfigLoaded();
  state.config.server = state.config.server || {};
  state.config.server[key] = value;
  markDirty(true);
}

function updateGuiField(key, value) {
  ensureConfigLoaded();
  state.config.server = state.config.server || {};
  state.config.server.gui = state.config.server.gui || {};
  state.config.server.gui[key] = value;
  markDirty(true);
}

function updateGuiEnabledState() {
  const disabled = elements.guiEnabled.value === "false";
  elements.guiHost.disabled = disabled;
  elements.guiPort.disabled = disabled;
}

function markDirty(isDirty) {
  if (typeof isDirty === "boolean") {
    state.dirty = isDirty;
  } else {
    state.dirty = true;
  }
  updateSaveButtonState();
  updateTitle();
}

function updateSaveButtonState() {
  elements.btnSave.disabled = !state.dirty;
}

function updateTitle() {
  if (!state.baseTitle) {
    state.baseTitle = "VXI Proxy Configuration";
  }
  document.title = state.dirty ? `* ${state.baseTitle.replace(/^\*\s*/, "")}` : state.baseTitle;
}

function setStatus(message, variant = null) {
  if (state.statusTimer) {
    window.clearTimeout(state.statusTimer);
    state.statusTimer = null;
  }
  if (!message) {
    elements.status.textContent = "";
    elements.status.removeAttribute("data-variant");
    return;
  }
  elements.status.textContent = message;
  if (variant) {
    elements.status.dataset.variant = variant;
  } else {
    elements.status.removeAttribute("data-variant");
  }
  state.statusTimer = window.setTimeout(() => {
    elements.status.textContent = "";
    elements.status.removeAttribute("data-variant");
  }, 5000);
}

function coerceValue(raw) {
  if (typeof raw !== "string") {
    return raw;
  }
  const value = raw.trim();
  if (!value.length) {
    return "";
  }
  if (value === "true" || value === "false") {
    return value === "true";
  }
  if (/^-?\d+$/.test(value)) {
    const parsed = Number.parseInt(value, 10);
    if (!Number.isNaN(parsed)) {
      return parsed;
    }
  }
  if (/^-?\d*\.\d+$/.test(value)) {
    const parsed = Number.parseFloat(value);
    if (!Number.isNaN(parsed)) {
      return parsed;
    }
  }
  return raw;
}

function coerceNumber(raw) {
  const trimmed = String(raw ?? "").trim();
  if (!trimmed.length) {
    return 0;
  }
  const parsed = Number(trimmed);
  return Number.isNaN(parsed) ? 0 : parsed;
}

function stringifyValue(value) {
  if (value === null || value === undefined) {
    return "";
  }
  if (typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch (error) {
      return String(value);
    }
  }
  return String(value);
}

function ensureConfigLoaded() {
  if (!state.config) {
    throw new Error("Configuration not loaded yet");
  }
}

function validateConfig() {
  if (!state.config) {
    return "Configuration not loaded";
  }
  for (const [name, definition] of Object.entries(state.config.devices)) {
    if (!name.trim()) {
      return "Device name cannot be empty";
    }
    if (!definition.type || typeof definition.type !== "string") {
      return `Device ${name} is missing a type`;
    }
  }

  for (const [deviceName, rules] of Object.entries(state.config.mappings)) {
    if (!Array.isArray(rules)) {
      return `Mappings for ${deviceName} must be an array`;
    }
    for (const [index, rule] of rules.entries()) {
      if (!rule.pattern || !rule.pattern.trim()) {
        return `Mapping rule #${index + 1} for ${deviceName} must include a pattern`;
      }
      const deviceType = (state.config.devices[deviceName]?.type || "").toLowerCase();
      if (deviceType.startsWith("modbus") && !rule.action) {
        const hasStatic = !!(rule.params && typeof rule.params === "object" && typeof rule.params.response === "string" && rule.params.response.trim() !== "");
        if (!hasStatic) {
          return `Mapping rule #${index + 1} for ${deviceName} must include an action or a response`;
        }
      }
    }
  }
  return null;
}
