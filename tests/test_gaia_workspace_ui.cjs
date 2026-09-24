/* Full one-button workflow with an offline DOM and HTTP fixtures. No real submission. */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'panel.html'), 'utf8');
const scripts = ['gaia-execution.js', 'gaia-workspace.js'].map(name =>
  fs.readFileSync(path.join(root, 'assets', name), 'utf8'));
const tick = () => new Promise(resolve => setImmediate(resolve));

async function fixture(override = () => undefined) {
  const elements = {}, handlers = {}, calls = [], timers = [], storage = new Map();
  for (const [, id] of html.matchAll(/id="(gaia[^"]+)"/g)) {
    assert.equal(elements['#' + id], undefined, 'no duplicate Gaia element IDs');
    const events = handlers['#' + id] = {};
    elements['#' + id] = {value: '', disabled: false, hidden: false, textContent: '', files: [],
      addEventListener(name, fn) {events[name] = fn;}, click() {return events.click?.();}};
  }
  elements['#gaiaShop'].value = '7970406';
  elements['#gaiaScene'].value = '2';
  elements['#gaiaCondition'].value = '99新';
  elements['#gaiaModels'].value = '苹果,iPhone 17 Pro';
  const mapping = {id: 'a'.repeat(32), name: '价格规则.xlsx', sha256: 'b'.repeat(64),
    record_count: 2, mainland_filter_applied: true, mapping_policy: 'explicit_mainland_only_v2'};
  const preview = {rows: [], row_count: 2, quality_label: '99新', quality_id: '99',
    mapping_version_id: mapping.id, source_sha256: 'template-hash'};
  const batch = {id: 'abc123', shop_id: '7970406', output_hash: 'output-hash',
    state: 'validation_passed', summary: {generated_rows: 2}};
  const accepted = {...batch, state: 'gaia_accepted', gaia_receipt: {
    execution_key: 'key', phase: 'accepted', message: '盖亚已受理，未启动抖店发布'}};
  const defaults = {
    '/api/gaia/capabilities': {success: true, features: {gaia_submit: true}, contracts: {mapping_policy: 'explicit_mainland_only_v2'}},
    '/api/gaia/qualities/status': {success: true, qualities: {'99新': {configured: true}}},
    '/api/gaia/model-mappings/latest': {success: true, mapping},
    '/api/gaia/excel/latest': {success: true, warnings: [{source: 'downloads', message: 'Permission denied'}], files: [
      {path: '/imports/template.xlsx', name: '模板.xlsx', source_kind: 'managed_template', status: 'recognized', profile_matches: [{id: 'gaia_publish_template'}]},
      {path: '/imports/mapping.xlsx', name: '副本.xlsx', source_kind: 'managed_mapping', status: 'recognized', profile_matches: [{id: 'gaia_goods_price_mapping'}]},
    ]},
    '/api/gaia/excel/inspect': {success: true, inspection: {status: 'recognized', profile_matches: [{id: 'gaia_publish_template', name: '双维模板'}], sheets: []}},
    '/api/gaia/model-mappings/import-download': {success: true, mapping, reused: true},
    '/api/gaia/model-mappings': {success: true, mapping},
    '/api/gaia/model-mappings/resolve-batch': {success: true, selected_sku_ids: ['sku1', 'sku2'], results: [{
      index: 1, brand_input: '苹果', model_input: 'iPhone 17 Pro', unique_model: true, candidate_count: 2,
      candidates: [{brand: '苹果', model: 'iPhone 17 Pro'}],
    }], message: '匹配成功'},
    '/api/gaia/batches/preview': {success: true, preview},
    '/api/gaia/batches/generate': {success: true, batch, preview, download_url: '/file.xlsx'},
    '/api/gaia/batches/abc123/handoff-preflight?shop_id=7970406': {success: true},
    '/api/gaia/batches/abc123/start': {success: true, batch: accepted},
    '/api/gaia/batches/abc123': {success: true, batch: accepted},
  };
  const context = {window: {addEventListener() {}}, document: {querySelector: key => elements[key]}, AbortController,
    FormData: class {append() {}},
    sessionStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)},
    setTimeout: fn => {timers.push(fn); return timers.length;}, clearTimeout() {},
    fetch: async (url, options) => {
      calls.push({url, options});
      const data = await override(url, options, defaults) ?? defaults[url];
      assert.ok(data, 'unexpected endpoint: ' + url);
      return {status: data.success ? 200 : 400, json: async () => data};
    },
  };
  vm.createContext(context);
  scripts.forEach(script => vm.runInContext(script, context));
  context.window.initGaiaWorkspace({$: key => elements[key], toast() {}});
  await tick();
  return {elements, handlers, calls, defaults, timers,
    start: () => handlers['#gaiaStartBtn'].click(),
    count: suffix => calls.filter(call => call.url.endsWith(suffix)).length};
}

test('one click automatically loads, matches, generates, verifies, starts and polls', async () => {
  const f = await fixture();
  assert.equal(f.count('/start'), 0, 'loading the page must never submit');
  await f.start();
  assert.equal(f.count('/excel/latest'), 1, 'share one file discovery per run');
  for (const endpoint of ['/resolve-batch', '/preview', '/generate', '/start']) assert.equal(f.count(endpoint), 1);
  const request = f.calls.find(call => call.url.endsWith('/generate'));
  assert.deepEqual(JSON.parse(request.options.body).selected_sku_ids, ['sku1', 'sku2']);
  assert.match(f.elements['#gaiaSelectedMappingFile'].textContent, /使用已保存版本/);
  assert.equal(f.elements['#gaiaGenerationStatus'].textContent, '盖亚已受理');
  assert.match(f.elements['#gaiaUploadStatus'].textContent, /未启动抖店发布/);
  assert.equal(f.elements['#gaiaFormFields'].disabled, false);
  assert.equal(f.elements['#gaiaStartBtn'].disabled, true);
  assert.equal(f.elements['#gaiaStopBtn'].textContent, '已提交，无法撤回');
  assert.equal(f.elements['#gaiaStopBtn'].disabled, true);
  assert.match(f.elements['#gaiaStopHint'].textContent, /本地停止不能撤回/);
  await f.start();
  assert.equal(f.count('/start'), 1, 'completed batch cannot be submitted twice');
});

test('rapid repeated starts join no duplicate work', async () => {
  const f = await fixture();
  await Promise.all(Array.from({length: 10}, () => f.start()));
  assert.equal(f.count('/generate'), 1);
  assert.equal(f.count('/start'), 1);
});

test('Stop is outside every disabled fieldset and next to Start', () => {
  const offset = html.indexOf('id="gaiaStopBtn"');
  const before = html.slice(0, offset);
  assert.equal((before.match(/<fieldset\b/g) || []).length, (before.match(/<\/fieldset>/g) || []).length);
  assert.match(before.slice(-500), /id="gaiaStartBtn"/);
});

for (const stage of ['/excel/inspect', '/import-download', '/resolve-batch', '/preview', '/generate', '/handoff-preflight?shop_id=7970406']) {
  test('Stop during preparation never starts browser: ' + stage, async () => {
    let release, cancelled, pending = false;
    const wait = new Promise(resolve => {release = resolve;});
    const f = await fixture(async (url, options, defaults) => {
      if (url.endsWith(stage)) {pending = true; await wait;}
      if (url.endsWith('/stop')) {
        cancelled = {...defaults['/api/gaia/batches/generate'].batch, state: 'cancelled',
          gaia_receipt: {phase: 'stopped', message: '已停止，未提交盖亚'}};
        return {success: true, batch: cancelled};
      }
      if (url === '/api/gaia/batches/abc123' && cancelled) return {success: true, batch: cancelled};
    });
    const running = f.start();
    await tick();
    assert.equal(pending, true);
    assert.equal(f.elements['#gaiaStopBtn'].disabled, false);
    assert.equal(f.elements['#gaiaAdvancedFields'].disabled, true);
    await f.handlers['#gaiaStopBtn'].click();
    assert.equal(f.elements['#gaiaStopBtn'].disabled, true);
    assert.equal(f.elements['#gaiaStopBtn'].textContent, '正在停止…');
    assert.match(f.elements['#gaiaStopHint'].textContent, /已阻止后续上传/);
    release();
    await running;
    assert.equal(f.count('/start'), 0);
    assert.equal(f.count('/stop'), ['/generate', '/handoff-preflight?shop_id=7970406'].includes(stage) ? 1 : 0);
    assert.match(f.elements['#gaiaUploadStatus'].textContent, /已停止/);
    assert.equal(f.elements['#gaiaStopBtn'].textContent, '已停止');
    assert.equal(f.elements['#gaiaFormFields'].disabled, false);
    assert.equal(f.elements['#gaiaAdvancedFields'].disabled, false);
  });
}

test('browser Stop displays feedback immediately and stays stopping during cleanup', async () => {
  let release, serverBatch, posts = 0;
  const wait = new Promise(resolve => {release = resolve;});
  const f = await fixture(async (url, options, defaults) => {
    if (url.endsWith('/start')) serverBatch = {...defaults['/api/gaia/batches/generate'].batch,
      state: 'confirmed', gaia_receipt: {execution_key: 'key', phase: 'uploading', submit_intent: false}};
    if (url.endsWith('/stop')) {
      posts++;
      await wait;
      serverBatch = {...serverBatch, state: 'cancelled', gaia_receipt: {
        ...serverBatch.gaia_receipt, phase: 'stopping', message: '已阻止提交，正在关闭本批次浏览器…'}};
    }
    if (url.endsWith('/start') || url.endsWith('/stop') || url === '/api/gaia/batches/abc123') {
      return {success: true, batch: serverBatch};
    }
  });
  await f.start();
  assert.equal(f.elements['#gaiaStopBtn'].disabled, false);
  const stop = f.handlers['#gaiaStopBtn'].click();
  assert.equal(f.elements['#gaiaStopBtn'].textContent, '正在停止…');
  assert.match(f.elements['#gaiaStopHint'].textContent, /已发送停止请求/);
  await f.handlers['#gaiaStopBtn'].click();
  assert.equal(posts, 1);
  release();
  await stop;
  assert.equal(f.elements['#gaiaStopBtn'].textContent, '正在停止…');
  assert.match(f.elements['#gaiaStopHint'].textContent, /已阻止提交/);
  serverBatch.gaia_receipt = {...serverBatch.gaia_receipt, phase: 'stopped', message: '已停止，未提交盖亚'};
  await f.timers.at(-1)();
  assert.equal(f.elements['#gaiaStopBtn'].textContent, '已停止');
  assert.equal(f.elements['#gaiaGenerationStatus'].textContent, '已停止');
  assert.equal(f.elements['#gaiaAdvancedFields'].disabled, false);
});

for (const failure of ['submitted', 'offline']) {
  test('preparation cancellation never claims stopped when result is ' + failure, async () => {
    let release, waiting = false, stopAttempted = false;
    const wait = new Promise(resolve => {release = resolve;});
    const f = await fixture(async (url, options, defaults) => {
      if (url.endsWith('/generate')) {waiting = true; await wait;}
      if (url.endsWith('/stop')) {
        stopAttempted = true;
        if (failure === 'offline') throw new Error('offline');
        return {success: false, message: '已进入提交阶段，无法撤回'};
      }
      if (url === '/api/gaia/batches/abc123' && stopAttempted) {
        if (failure === 'offline') throw new Error('offline');
        return {success: true, batch: {...defaults['/api/gaia/batches/generate'].batch,
          state: 'gaia_submitted', gaia_receipt: {execution_key: 'key', submit_intent: true, phase: 'submitting'}}};
      }
    });
    const running = f.start();
    await tick();
    assert.ok(waiting);
    await f.handlers['#gaiaStopBtn'].click();
    release();
    await running;
    assert.equal(f.count('/start'), 0);
    assert.doesNotMatch(f.elements['#gaiaGenerationStatus'].textContent, /已停止/);
    assert.doesNotMatch(f.elements['#gaiaUploadStatus'].textContent, /已停止/);
    assert.equal(f.elements['#gaiaStopBtn'].textContent, failure === 'offline' ? '停止待确认' : '已提交，无法撤回');
    assert.equal(f.elements['#gaiaStopBtn'].disabled, true);
    assert.equal(f.elements['#gaiaStartBtn'].disabled, true);
  });
}

for (const stage of ['/excel/inspect', '/import-download', '/resolve-batch', '/preview', '/generate', '/handoff-preflight?shop_id=7970406']) {
  test('failure stops before upload and persists its reason: ' + stage, async () => {
    const f = await fixture(url => url.endsWith(stage) ? {success: false, message: '校验不通过：测试原因'} : undefined);
    await f.start();
    assert.equal(f.count('/start'), 0);
    assert.match(f.elements['#gaiaUploadStatus'].textContent, /已停止/);
    assert.equal(f.elements['#gaiaResultDetails'].open, true);
    assert.equal(f.elements['#gaiaFormFields'].disabled, false);
    assert.equal(f.elements['#gaiaStartBtn'].disabled, false, 'failure remains recoverable');
  });
}

test('after a handoff failure another explicit start reuses the same file', async () => {
  let blocked = true;
  const f = await fixture(url => blocked && url.includes('/handoff-preflight') ? {success: false, message: '暂时阻断'} : undefined);
  await f.start();
  blocked = false;
  await f.start();
  assert.equal(f.count('/generate'), 1);
  assert.equal(f.count('/start'), 1);
});

test('selecting a mapping file needs no separate upload click', async () => {
  const f = await fixture();
  f.elements['#gaiaMappingFile'].files = [{name: '新价格规则.xlsx', size: 2048}];
  f.handlers['#gaiaMappingFile'].change();
  await f.start();
  assert.equal(f.count('/model-mappings'), 1);
  assert.equal(f.count('/import-download'), 0);
  assert.equal(f.count('/start'), 1);
});

test('failed manual upload never falls back silently to another mapping', async () => {
  const f = await fixture(url => url.endsWith('/model-mappings') ? {success: false, message: '误选了发品模板，请上传价格规则表'} : undefined);
  f.elements['#gaiaMappingFile'].files = [{name: '错误表.xlsx', size: 2048}];
  f.handlers['#gaiaMappingFile'].change();
  await f.start();
  assert.equal(f.count('/import-download'), 0);
  assert.equal(f.count('/start'), 0);
  assert.match(f.elements['#gaiaUploadStatus'].textContent, /误选了发品模板/);
});

test('missing operator parameters do not generate or submit anything', async () => {
  const f = await fixture();
  f.elements['#gaiaShop'].value = '';
  await f.start();
  assert.equal(f.count('/excel/latest'), 0);
  assert.equal(f.count('/generate'), 0);
  assert.equal(f.count('/start'), 0);
});

test('backend disabled gate cannot be bypassed by a cached mapping', async () => {
  const f = await fixture((url, options, defaults) => url.endsWith('/capabilities')
    ? {...defaults[url], features: {gaia_submit: false}} : undefined);
  assert.equal(f.elements['#gaiaStartBtn'].disabled, true);
  await f.start();
  assert.equal(f.count('/start'), 0);
});

test('new inputs after an accepted task permit a new, explicit start', async () => {
  const f = await fixture();
  await f.start();
  f.elements['#gaiaModels'].value = 'OPPO,Find X8';
  f.handlers['#gaiaModels'].input();
  assert.equal(f.elements['#gaiaStartBtn'].disabled, false);
  assert.equal(f.count('/start'), 1, 'input alone never submits');
});

test('only one normal start button is exposed, diagnostic buttons stay in collapsed details', () => {
  assert.match(html, /<details[^>]*id="gaiaAdvanced">/);
  const beforeAdvanced = html.split('id="gaiaAdvanced"')[0].split('id="gaiaView"')[1];
  for (const id of ['gaiaResolveModelsBtn', 'gaiaParseBtn', 'gaiaSubmitBtn']) assert.ok(!beforeAdvanced.includes('id="' + id + '"'));
  assert.ok(beforeAdvanced.includes('id="gaiaStartBtn"'));
});
