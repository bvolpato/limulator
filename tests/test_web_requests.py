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
    catalog: {}, operations: new Map(),
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
