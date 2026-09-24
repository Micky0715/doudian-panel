/* Offline DOM fixture tests. These do not open or control a real browser. */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const script = fs.readFileSync(path.join(__dirname, '../assets/gaia-execution.js'), 'utf8');

function fixture(fetch, {previous} = {}) {
  const elements = {};
  const events = {};
  for (const selector of ['#gaiaSubmitBtn', '#gaiaUploadStatus', '#gaiaFormFields']) {
    elements[selector] = {disabled: false, textContent: '', addEventListener: (name, fn) => {events[selector + name] = fn;}};
  }
  const storage = new Map();
  if (previous) storage.set('gaia-upload-batch', previous);
  const timers = [];
  const pendingTimers = new Map();
  const window = {addEventListener(name, fn) {events[name] = fn;}};
  vm.runInNewContext(script, {
    window, document: {querySelector: key => elements[key]}, fetch, AbortController,
    sessionStorage: {getItem: key => storage.get(key), setItem: (key, val) => storage.set(key, val), removeItem: key => storage.delete(key)},
    setTimeout: (fn, delay) => {timers.push(fn); pendingTimers.set(timers.length, {fn, delay}); return timers.length;},
    clearTimeout: id => pendingTimers.delete(id),
  });
  const controller = window.createGaiaUploadController();
  const batch = {id: 'abc123', shop_id: '7970406', state: 'validation_passed', output_hash: 'hash', summary: {generated_rows: 45}};
  controller.setEnabled(true);
  controller.setBatch(batch);
  return {controller, elements, events, timers, pendingTimers, batch};
}
const response = data => ({status: 200, json: async () => data});

test('one explicit click starts the generated batch and reports accepted, not Doudian publication', async () => {
  const calls = [];
  let ctx;
  ctx = fixture(async (url, options) => {
    calls.push({url, options});
    const data = {...ctx.batch, state: 'gaia_accepted', gaia_receipt: {execution_key: 'key', phase: 'accepted', message: '盖亚已受理，未启动抖店发布'}};
    return response({success: true, batch: data});
  });
  await ctx.events['#gaiaSubmitBtnclick']();
  await ctx.events['#gaiaSubmitBtnclick']();
  assert.equal(calls.filter(call => call.options?.method === 'POST').length, 1);
  assert.equal(JSON.parse(calls[0].options.body).expected_output_hash, 'hash');
  assert.equal(ctx.elements['#gaiaFormFields'].disabled, false);
  assert.equal(ctx.elements['#gaiaSubmitBtn'].disabled, true);
  assert.match(ctx.elements['#gaiaUploadStatus'].textContent, /未启动抖店发布/);
});

test('lost start response polls the original batch without a second POST', async () => {
  let posts = 0, ctx;
  ctx = fixture(async (url, options) => {
    if (options?.method === 'POST') {posts++; throw new Error('connection lost');}
    return response({success: true, batch: {...ctx.batch, state: 'gaia_submitted',
      gaia_receipt: {execution_key: 'key', phase: 'submitting', message: '等待盖亚反馈'}}});
  });
  await ctx.events['#gaiaSubmitBtnclick']();
  await ctx.events['#gaiaSubmitBtnclick']();
  assert.equal(posts, 1);
  assert.equal(ctx.controller.isBusy(), true);
  assert.equal(ctx.elements['#gaiaFormFields'].disabled, true);
  assert.equal(ctx.pendingTimers.size, 1);
});

test('cancelled batch with old upload phase is terminal, not an endless running task', async () => {
  let ctx;
  ctx = fixture(async () => response({success: true, batch: {...ctx.batch, state: 'cancelled',
    gaia_receipt: {execution_key: 'key', phase: 'uploading', message: '已取消'}}}));
  await ctx.events['#gaiaSubmitBtnclick']();
  assert.equal(ctx.controller.isBusy(), false);
  assert.equal(ctx.pendingTimers.size, 0);
});

test('clearing a generated file disables upload', () => {
  const ctx = fixture(async () => {throw new Error('must not fetch');});
  ctx.controller.setBatch(null);
  assert.equal(ctx.elements['#gaiaSubmitBtn'].disabled, true);
});

test('Stop takes over an in-flight Start without allowing a stale response to revive it', async () => {
  let ctx, releaseStart, releaseStop, serverBatch;
  const startWait = new Promise(resolve => {releaseStart = resolve;});
  const stopWait = new Promise(resolve => {releaseStop = resolve;});
  let posts = 0;
  ctx = fixture(async (url) => {
    if (url.endsWith('/start')) {await startWait; return response({success: true, batch: {
      ...ctx.batch, state: 'confirmed', gaia_receipt: {phase: 'uploading', execution_key: 'key'}}});}
    if (url.endsWith('/stop')) {
      posts++;
      await stopWait;
      serverBatch = {...ctx.batch, state: 'cancelled', gaia_receipt: {phase: 'stopped', message: '已停止，未提交盖亚'}};
    }
    return response({success: true, batch: serverBatch});
  });
  const start = ctx.controller.start();
  const stop = ctx.controller.stop();
  await ctx.controller.stop();
  assert.equal(posts, 1, 'rapid Stop is idempotent');
  releaseStop();
  await stop;
  releaseStart();
  await start;
  assert.equal(ctx.controller.isBusy(), false);
  assert.equal(ctx.controller.canStart(), false);
  assert.match(ctx.elements['#gaiaUploadStatus'].textContent, /已停止/);
});

test('Stop loses a race to submit intent and reports that it cannot revoke Gaia submission', async () => {
  let ctx;
  ctx = fixture(async url => url.endsWith('/stop')
    ? response({success: false, message: '已提交盖亚，无法撤回'})
    : response({success: true, batch: {...ctx.batch, state: 'gaia_submitted',
      gaia_receipt: {phase: 'submitting', submit_intent: true, message: '等待反馈'}}}));
  const stopped = await ctx.controller.stop();
  assert.equal(stopped.success, false);
  assert.equal(ctx.controller.isBusy(), true);
  assert.equal(ctx.controller.canStop(), false);
  assert.match(ctx.elements['#gaiaUploadStatus'].textContent, /无法撤回/);
});

test('Stop remains busy until browser cleanup ends and polling a cancelled batch finishes', async () => {
  let ctx, phase = 'stopping';
  ctx = fixture(async () => response({success: true, batch: {...ctx.batch, state: 'cancelled',
    gaia_receipt: {phase, message: '已阻止提交'}}}));
  await ctx.controller.stop();
  assert.equal(ctx.controller.isBusy(), true);
  assert.equal(ctx.controller.canStop(), false);
  phase = 'stopped';
  await ctx.timers.at(-1)();
  assert.equal(ctx.controller.isBusy(), false);
});

test('a stale uploading poll cannot overwrite a newer stopped receipt', async () => {
  let ctx, reads = 0, release;
  const wait = new Promise(resolve => {release = resolve;});
  ctx = fixture(async (url) => {
    if (!url.endsWith('/stop') && !url.endsWith('/start')) {
      reads++;
      if (reads === 2) {await wait; return response({success: true, batch: active});}
    }
    return response({success: true, batch: url.endsWith('/stop') || reads > 2 ? stopped : active});
  });
  const active = {...ctx.batch, state: 'confirmed', gaia_receipt: {phase: 'uploading', execution_key: 'key'}};
  const stopped = {...ctx.batch, state: 'cancelled', gaia_receipt: {phase: 'stopped', message: '已停止'}};
  await ctx.controller.start();
  const stale = ctx.timers.at(-1)();
  await ctx.controller.stop();
  release();
  await stale;
  assert.equal(ctx.controller.isBusy(), false);
  assert.match(ctx.elements['#gaiaUploadStatus'].textContent, /已停止/);
});

test('Stop view distinguishes idle, available, stopping, stopped and submitted', async () => {
  let ctx, release;
  const wait = new Promise(resolve => {release = resolve;});
  ctx = fixture(async url => {
    if (url.endsWith('/stop')) await wait;
    return response({success: true, batch: {...ctx.batch, state: 'cancelled',
      gaia_receipt: {phase: 'stopped', message: '已停止'}}});
  });
  assert.equal(ctx.controller.stopStatus().kind, 'available');
  assert.equal(ctx.controller.stopStatus().enabled, true);
  const stop = ctx.controller.stop();
  assert.equal(ctx.controller.stopStatus().label, '正在停止…');
  assert.equal(ctx.controller.stopStatus().enabled, false);
  release();
  await stop;
  assert.equal(ctx.controller.stopStatus().label, '已停止');
  ctx.controller.setBatch(null);
  assert.equal(ctx.controller.stopStatus().kind, 'idle');
  for (const state of ['gaia_submitted', 'gaia_accepted', 'gaia_rejected', 'manual_required']) {
    ctx.controller.setBatch({...ctx.batch, state, gaia_receipt: {submit_intent: true}});
    assert.equal(ctx.controller.stopStatus().label, '已提交，无法撤回');
    assert.equal(ctx.controller.stopStatus().enabled, false);
  }
});

test('lost Stop response reconciles a confirmed cancellation instead of reporting failure', async () => {
  let ctx;
  ctx = fixture(async url => {
    if (url.endsWith('/stop')) throw new Error('lost POST response');
    return response({success: true, batch: {...ctx.batch, state: 'cancelled',
      gaia_receipt: {phase: 'stopped', message: '已停止，未提交盖亚'}}});
  });
  const stopped = await ctx.controller.stop();
  assert.equal(stopped.success, true);
  assert.equal(ctx.controller.stopStatus().label, '已停止');
  assert.match(ctx.elements['#gaiaUploadStatus'].textContent, /已停止，未提交盖亚/);
  assert.equal(ctx.pendingTimers.size, 0);
});

test('Stop transport timeout stays uncertain until a read confirms cancellation', async () => {
  let ctx, offline = true, posts = 0;
  ctx = fixture(async (url, options) => {
    if (url.endsWith('/stop')) {
      posts++;
      return new Promise((resolve, reject) => {
        options.signal.addEventListener('abort', () => reject(new Error('timeout')), {once: true});
      });
    }
    if (offline) throw new Error('read disconnected');
    return response({success: true, batch: {...ctx.batch, state: 'cancelled',
      gaia_receipt: {phase: 'stopped', message: '已停止，未提交盖亚'}}});
  });
  const stop = ctx.controller.stop();
  const timeout = [...ctx.pendingTimers.values()].find(item => item.delay === 10000);
  assert.ok(timeout, 'Stop must not wait forever on an unresponsive HTTP request');
  timeout.fn();
  const result = await stop;
  assert.equal(result.success, false);
  assert.equal(ctx.controller.stopStatus().label, '停止待确认');
  assert.equal(ctx.controller.isBusy(), true);
  assert.equal(ctx.controller.canStart(), false);
  assert.equal(ctx.elements['#gaiaFormFields'].disabled, true);
  assert.doesNotMatch(ctx.elements['#gaiaUploadStatus'].textContent, /已停止/);
  await ctx.controller.start();
  await ctx.controller.stop();
  assert.equal(posts, 1, 'do not pile up Start or Stop while status is unknown');
  offline = false;
  await ctx.timers.at(-1)();
  assert.equal(ctx.controller.stopStatus().label, '已停止');
  assert.equal(ctx.controller.isBusy(), false);
});

test('404 after a lost Stop is not proof that the task stopped', async () => {
  const ctx = fixture(async url => {
    if (url.endsWith('/stop')) throw new Error('connection lost');
    return {status: 404, json: async () => ({success: false})};
  });
  await ctx.controller.stop();
  assert.equal(ctx.controller.stopStatus().label, '停止待确认');
  assert.equal(ctx.controller.isBusy(), true);
  assert.equal(ctx.controller.canStart(), false);
});

test('a late rejected Start request cannot overwrite an already confirmed Stop', async () => {
  let ctx, rejectStart, reads = 0;
  const pending = new Promise((resolve, reject) => {rejectStart = reject;});
  ctx = fixture(async url => {
    if (url.endsWith('/start')) return pending;
    if (!url.endsWith('/stop')) reads++;
    return response({success: true, batch: {...ctx.batch, state: 'cancelled',
      gaia_receipt: {phase: 'stopped', message: '已停止，未提交盖亚'}}});
  });
  const start = ctx.controller.start();
  await ctx.controller.stop();
  const readsAfterStop = reads;
  rejectStart(new Error('late timeout'));
  await start;
  assert.equal(reads, readsAfterStop, 'old Start must not start another poll');
  assert.match(ctx.elements['#gaiaUploadStatus'].textContent, /已停止/);
});

test('restored submitted session disables Stop after reading the original batch', async () => {
  const ctx = fixture(async () => response({success: true, batch: {
    id: 'abc123', shop_id: '7970406', output_hash: 'hash', state: 'gaia_submitted',
    gaia_receipt: {submit_intent: true, phase: 'submitting', execution_key: 'key'},
  }}), {previous: 'abc123'});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ctx.controller.stopStatus().label, '已提交，无法撤回');
  assert.equal(ctx.controller.isBusy(), true);
});

test('leaving the page cancels in-flight transport and does not schedule a new poll', async () => {
  let aborted = 0;
  const ctx = fixture(async (url, options) => new Promise((resolve, reject) => {
    options.signal.addEventListener('abort', () => {aborted++; reject(new Error('page hidden'));}, {once: true});
  }));
  const starting = ctx.controller.start();
  ctx.events.pagehide();
  await starting;
  assert.equal(aborted, 1);
  assert.equal(ctx.pendingTimers.size, 0);
});

test('a timed-out Start cannot restart while the server is still preparing, but can Stop', async () => {
  let ctx, starts = 0, stopped = false;
  ctx = fixture(async url => {
    if (url.endsWith('/start')) {starts++; throw new Error('start timeout');}
    if (url.endsWith('/stop')) stopped = true;
    return response({success: true, batch: stopped ? {...ctx.batch, state: 'cancelled',
      gaia_receipt: {phase: 'stopped', message: '已停止'}} : ctx.batch});
  });
  await ctx.controller.start();
  assert.equal(ctx.controller.isBusy(), true);
  assert.equal(ctx.controller.canStop(), true);
  await ctx.controller.start();
  assert.equal(starts, 1);
  await ctx.controller.stop();
  assert.equal(ctx.controller.stopStatus().label, '已停止');
});
