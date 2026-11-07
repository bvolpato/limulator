"""Execute the console's real request functions with stalled response bodies."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_console_body_deadlines_and_request_cleanup():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to execute the browser request functions")
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[2], 'utf8');
function section(start, end) {
  const offset = source.indexOf(start);
  return source.slice(offset, source.indexOf(end, offset));
}
const code = section('  function readErrorMessage(', '  function scheduleStatus(')
  + section('  async function loadDevices(', '  async function pollStatus(')
  + section('  async function mutate(', '  async function downloadScreen(');
function setup(mode) {
  const timers = new Set();
  const observed = { bodyStarted: false, aborted: false, notices: [] };
  const context = {
    AbortController, FormData,
    STATUS_TIMEOUT_MS: 20, ACTION_TIMEOUT_MS: 20,
    selectedName: 'duo', selectionEpoch: 0, catalogInFlight: false,
    statusTimer: null, screenTimer: null,
    catalog: {}, operations: new Map(), pendingReads: new Map(),
    els: { refreshDevices: {}, destinationSummary: {} },
    window: {
      setTimeout: (...args) => { const timer = setTimeout(...args); timers.add(timer); return timer; },
      clearTimeout: (timer) => { timers.delete(timer); clearTimeout(timer); },
    },
    fetch: async (_url, { signal }) => {
      if (mode === 'headers-error') throw new Error('headers read failed');
      return {
        ok: mode !== 'http-error', status: 409, statusText: 'Conflict',
        text: () => {
          observed.bodyStarted = true;
          if (mode === 'http-error') return Promise.resolve('{"detail":"Device is busy"}');
          if (mode === 'invalid-json') return Promise.resolve('{');
          if (mode === 'body-error') return Promise.reject(new Error('body read failed'));
          return new Promise((_resolve, reject) => signal.addEventListener('abort', () => {
            observed.aborted = true;
            reject(new DOMException('body aborted', 'AbortError'));
          }, { once: true }));
        },
      };
    },
    stringValue: (value) => typeof value === 'string' ? value.trim() : '',
    mutationHeaders: () => ({}), deviceConnected: () => true,
    deviceUrl: (path) => path, selectionIsCurrent: () => true,
    syncOperation() {}, resetPolling() {}, updateControls() {}, renderStatus() {},
    scheduleStatus() {}, scheduleScreen() {},
    showNotice: (message) => observed.notices.push(message),
  };
  vm.createContext(context);
  vm.runInContext(code, context);
  return { context, observed, timers };
}
async function bounded(promise) {
  let deadline;
  try {
    return await Promise.race([promise, new Promise((_resolve, reject) => {
      deadline = setTimeout(() => reject(new Error('body deadline did not settle request')), 150);
    })]);
  } finally { clearTimeout(deadline); }
}
(async () => {
  for (const target of ['mutation', 'catalog']) {
    const { context, observed, timers } = setup('stall');
    await bounded(target === 'mutation' ? context.mutate('/api/start') : context.loadDevices());
    assert.equal(observed.bodyStarted, true);
    assert.equal(observed.aborted, true);
    assert(observed.notices.some((message) => /Request timed out after/.test(message)));
    assert.equal(context.operations.size, 0);
    assert.equal(context.catalogInFlight, false);
    assert.equal(timers.size, 0);
  }
  for (const mode of ['headers-error', 'http-error', 'body-error']) {
    for (const target of ['mutation', 'catalog']) {
      const { context, observed, timers } = setup(mode);
      await bounded(target === 'mutation' ? context.mutate('/api/start') : context.loadDevices());
      assert.equal(observed.bodyStarted, mode !== 'headers-error');
      assert.equal(context.operations.size, 0);
      assert.equal(context.catalogInFlight, false);
      assert.equal(timers.size, 0);
      assert(observed.notices.some((message) => /Device is busy|body read failed|headers read failed/.test(message)));
    }
  }
  const { context, observed, timers } = setup('invalid-json');
  await bounded(context.loadDevices());
  assert.equal(context.catalogInFlight, false);
  assert.equal(timers.size, 0);
  assert(observed.notices.some((message) => /Device catalog is unavailable/.test(message)));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    source = Path(__file__).resolve().parents[1] / "src/limulator/web/app.js"
    result = subprocess.run([node, "-", str(source)], input=script, text=True, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stdout + result.stderr


def test_console_drains_reads_before_commands_and_unlocks_explicitly():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to execute the browser request functions")
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[2], 'utf8');
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const flush = () => new Promise((resolve) => setImmediate(resolve));
function setup(stalledPaths = []) {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      value: '', hidden: false, textContent: '', dataset: {}, listeners: {},
      addEventListener(type, listener) { this.listeners[type] = listener; },
      removeAttribute() {}, classList: { toggle() {} },
    });
    return elements.get(id);
  }
  elements.set('app-selector', {
    children: [], selectedIndex: -1,
    get value() { return this.children[this.selectedIndex]?.value || ''; },
    set value(value) { this.selectedIndex = this.children.findIndex((option) => option.value === value); },
    replaceChildren() { this.children = []; this.selectedIndex = -1; },
    append(option) { this.children.push(option); if (this.selectedIndex < 0) this.selectedIndex = 0; },
    addEventListener() {},
  });
  const timers = new Map();
  const calls = [], notices = [], screens = [], reads = [], commands = [];
  let nextTimer = 0;
  const context = {
    AbortController, FormData, Blob, URL, Uint8Array, console,
    window: {
      location: { origin: 'http://console.test' },
      setTimeout(callback, delay) { const id = ++nextTimer; timers.set(id, { callback, delay }); return id; },
      clearTimeout(id) { timers.delete(id); },
    },
    document: {
      hidden: false, getElementById: element,
      createElement: () => ({ value: '', textContent: '' }),
      querySelector: () => ({ content: 'session-token' }),
      querySelectorAll: () => [], addEventListener() {},
    },
    fetch: async (path, options) => {
      const url = new URL(path, 'http://console.test');
      const call = { path: url.pathname, name: url.searchParams.get('device'), options };
      calls.push(call);
      if (options.method === 'POST') {
        const body = deferred();
        const response = { ok: true, status: 409, statusText: 'Conflict', text: () => body.promise };
        commands.push({ ...call, body, response });
        return response;
      }
      if (url.pathname === '/api/screen.png') {
        const body = deferred();
        options.signal.addEventListener('abort', () => body.reject(new DOMException('aborted', 'AbortError')));
        screens.push({ ...call, body });
        return { ok: true, headers: { get: () => 'frame' }, blob: () => body.promise };
      }
      if (stalledPaths.includes(url.pathname)) {
        const body = deferred();
        reads.push({ ...call, body });
        return { ok: true, text: () => body.promise };
      }
      return { ok: true, text: async () => JSON.stringify(url.pathname === '/api/apps'
        ? { apps: [] } : { state: 'connected', device: {} }) };
    },
  };
  vm.createContext(context);
  vm.runInContext(source.replace('  initialize();', `
    renderStatus = () => { updateAppsView(); updateControls(); };
    updateDestination = clearScreen = () => {};
    decodeImage = async () => ({ width: 1, height: 1 });
    showNotice = (message, isError) => globalThis.notices.push({ message, isError });
    globalThis.api = {
      mutate, pollScreen, pollStatus, loadApps, bindEvents, updateControls, selectDestination,
      select(name) {
        selectDestination('device:' + name);
        deviceStatus = { state: 'connected', device: {} };
        updateControls();
      },
      disconnect() { deviceStatus = { state: 'offline' }; updateControls(); },
      apps(apps) { installedApps = apps; updateAppsView(); updateControls(); },
      get operationCount() { return operations.size; },
      get selectedName() { return selectedName; },
    };
    catalog.devices = ['duo', 'phone'].map((name) => ({ name }));
  `), Object.assign(context, { notices }));
  return { api: context.api, calls, screens, reads, commands, notices, timers, element };
}
const png = () => new Blob([new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10])]);
(async () => {
  {
    const { api, calls, screens, commands, timers } = setup();
    api.select('duo');
    const screen = api.pollScreen();
    await flush();
    const command = api.mutate('/api/launch', { body: { bundle_id: 'com.brutv' } });
    await flush();
    assert.equal(commands.length, 0, 'launch must wait for the active screen response body');
    await Promise.all([api.pollScreen(), api.pollStatus(), api.loadApps()]);
    assert.equal(calls.length, 1, 'no new device reads may start while launch waits');
    assert.equal(screens[0].options.signal.aborted, false, 'draining must not abort server work');
    screens[0].body.resolve(png());
    await screen;
    await flush();
    assert.equal(commands.length, 1);
    await Promise.all([api.pollScreen(), api.pollStatus(), api.loadApps()]);
    assert.equal(calls.length, 2, 'reads must stay paused until the mutation body completes');
    commands[0].body.resolve('{}');
    assert.equal(await command, true);
    assert.equal(api.operationCount, 0);
    assert([...timers.values()].some(({ delay }) => delay === 0), 'polling must resume after completion');
    await api.pollStatus();
    assert(calls.some(({ path }) => path === '/api/status'));
  }
  for (const path of ['/api/status', '/api/apps']) {
    const { api, reads, commands, timers } = setup([path]);
    api.select('duo');
    const read = path === '/api/status' ? api.pollStatus() : api.loadApps();
    await flush();
    if (path === '/api/apps') {
      assert([...timers.values()].some(({ delay }) => delay === 75000), 'apps must allow the 60s host RPC');
    }
    const command = api.mutate('/api/launch');
    await flush();
    assert.equal(commands.length, 0, `launch must drain ${path} as well as screen capture`);
    reads[0].body.resolve(JSON.stringify(path === '/api/status'
      ? { state: 'connected', device: {} } : { apps: [] }));
    await read;
    await flush();
    assert.equal(commands.length, 1);
    commands[0].body.resolve('{}');
    assert.equal(await command, true);
  }
  {
    const { api, reads, element } = setup(['/api/apps']);
    const apps = [{ bundle_id: 'com.brutv', name: 'BruTV' }, { bundle_id: 'com.settings', name: 'Settings' }];
    api.select('duo');
    api.apps(apps);
    const selector = element('app-selector');
    const launch = element('launch-app');
    selector.value = 'com.brutv';
    api.updateControls();
    assert.equal(launch.disabled, false);
    const refresh = api.loadApps();
    await flush();
    assert.equal(selector.value, 'com.brutv', 'refresh must preserve the selected installed app while loading');
    assert.equal(selector.disabled, true);
    reads[0].body.resolve(JSON.stringify({ apps }));
    await refresh;
    assert.equal(selector.value, 'com.brutv');
    assert.equal(launch.disabled, false);
    const removed = api.loadApps();
    reads[1].body.resolve(JSON.stringify({ apps: [apps[1]] }));
    await removed;
    assert.equal(selector.value, '', 'a removed app must lose its selection');
    assert.equal(launch.disabled, true);
    api.apps(apps);
    selector.value = 'com.brutv';
    const oldRefresh = api.loadApps();
    api.select('phone');
    assert.equal(selector.value, '', 'changing devices must clear the old selection');
    reads[2].body.resolve(JSON.stringify({ apps }));
    await oldRefresh;
    assert.equal(selector.value, '');
    assert.equal(selector.children.length, 1, 'the old response must not populate the new device');
    const phoneRefresh = api.loadApps();
    reads[3].body.resolve(JSON.stringify({ apps }));
    await phoneRefresh;
    assert.equal(selector.value, '', 'even a shared app identifier must not carry selection across devices');
    assert.equal(launch.disabled, true);
  }
  {
    const { api, screens, commands, notices, timers } = setup();
    api.select('duo');
    const screen = api.pollScreen();
    await flush();
    const command = api.mutate('/api/stop', { lifecycle: true });
    await flush();
    assert.equal(screens[0].options.signal.aborted, false);
    const deadline = [...timers.values()].find(({ delay }) => delay === 12000);
    assert(deadline, 'the active read must retain its timeout');
    deadline.callback();
    await screen;
    assert.equal(await command, false);
    assert.equal(commands.length, 0, 'a timed-out read must not be followed by a speculative mutation');
    assert.equal(api.operationCount, 0);
    assert(notices.some(({ message, isError }) => isError && /timed out/.test(message)));
  }
  {
    const { api, screens, commands, calls } = setup();
    api.select('duo');
    const screen = api.pollScreen();
    await flush();
    const oldCommand = api.mutate('/api/launch');
    api.select('phone');
    assert.equal(screens[0].options.signal.aborted, false, 'switching devices must drain the old request');
    await api.pollStatus();
    const newCommand = api.mutate('/api/input');
    await flush();
    assert.equal(commands.length, 1, 'an old device read must not block a different device');
    assert.equal(commands[0].name, 'phone');
    commands[0].body.resolve('{}');
    assert.equal(await newCommand, true);
    screens[0].body.resolve(png());
    await screen;
    await flush();
    assert.equal(commands.length, 2);
    assert.equal(commands[1].name, 'duo', 'the queued command must keep its original target');
    commands[1].body.resolve('{}');
    assert.equal(await oldCommand, true);
    assert.equal(api.selectedName, 'phone');
    assert.equal(api.operationCount, 0);
    assert(calls.some(({ path, name }) => path === '/api/status' && name === 'phone'));
  }
  {
    const { api, element, commands, notices, timers } = setup();
    api.bindEvents();
    api.select('phone');
    const unlock = element('unlock-device');
    assert.equal(unlock.disabled, false);
    unlock.listeners.click();
    await flush();
    assert.equal(commands.length, 1);
    assert.equal(commands[0].path, '/api/unlock');
    assert.equal(unlock.disabled, true);
    assert([...timers.values()].some(({ delay }) => delay === 150000), 'unlock must allow the 120s host RPC');
    commands[0].body.resolve('{"message":"Unlocked"}');
    await flush();
    assert.equal(unlock.disabled, false);
    assert(notices.some(({ message, isError }) => message === 'Unlocked' && !isError));
    unlock.listeners.click();
    await flush();
    commands[1].response.ok = false;
    commands[1].body.resolve('{"detail":"Unlock failed"}');
    await flush();
    assert.equal(unlock.disabled, false);
    assert(notices.some(({ message, isError }) => message === 'Unlock failed' && isError));
    api.disconnect();
    assert.equal(unlock.disabled, true);
    assert.equal(commands.length, 2, 'unlock must not retry or fall back to a Home command');
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    source = Path(__file__).resolve().parents[1] / "src/limulator/web/app.js"
    result = subprocess.run([node, "-", str(source)], input=script, text=True, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stdout + result.stderr
