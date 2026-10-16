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

test('installment choice is part of the main Gaia form; tools stay in advanced settings', () => {
  const form = html.match(/<fieldset id="gaiaFormFields"[\s\S]*?<\/fieldset>/)?.[0];
  const advanced = html.match(/<details class="workflow-card gaia-details gaia-advanced"[\s\S]*?<\/details>\s*<\/details>/)?.[0];
  assert.ok(form);
  assert.ok(advanced);
  assert.match(form, /id="gaiaPaymentOptions"/);
  assert.match(form, /id="gaiaActivityName"/);
  assert.match(form, /id="gaiaQualityLink"[^>]*><option value="1"[^>]*>是（表中填 1）<\/option><option value="0"[^>]*>否（表中填 0）/);
  assert.match(form, /value="full"/);
  assert.match(form, /value="6"/);
  assert.match(advanced, /id="gaiaPriceTemplate"/);
  assert.match(advanced, /id="gaiaPricePreview"/);
  assert.doesNotMatch(html, /<section class="workflow-card gaia-price-uplift"/);
});

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
  elements['#gaiaQualityLink'].value = '1';
  const paymentOptions = [
    {value: 'full', checked: false}, {value: '6', checked: false},
    {value: '12', checked: true}, {value: '24', checked: false},
  ];
  elements['#gaiaPaymentOptions'].options = paymentOptions;
  elements['#gaiaPaymentOptions'].querySelectorAll = selector =>
    selector.endsWith(':checked') ? paymentOptions.filter(option => option.checked) : paymentOptions;
  elements['#gaiaPaymentOptions'].choose = value => {
    const option = paymentOptions.find(item => item.value === value);
    option.checked = true;
    handlers['#gaiaPaymentOptions'].change({target: option});
  };
  elements['#gaiaCondition'].selectedValues = ['99新'];
  elements['#gaiaCondition'].querySelectorAll = () =>
    elements['#gaiaCondition'].selectedValues.map(value => ({value}));
  elements['#gaiaModels'].value = '苹果,iPhone 17 Pro';
  const mapping = {id: 'a'.repeat(32), name: '价格规则.xlsx', sha256: 'b'.repeat(64),
    record_count: 2, mainland_filter_applied: true, mapping_policy: 'explicit_mainland_only_v2',
    uploaded_at: '2026-10-09T00:00:00Z'};
  const template = {id: 'c'.repeat(32), name: '模板.xlsx', size: 2048,
    path: '/imports/template.xlsx', uploaded_at: '2026-10-09T00:00:00Z'};
  const preview = {rows: [], row_count: 2, quality_label: '99新', quality_id: '99', quality_link: '1',
    quality_labels: ['99新'], quality_ids: ['99'],
    mapping_version_id: mapping.id, source_sha256: 'template-hash'};
  const batch = {id: 'abc123', shop_id: '7970406', output_hash: 'output-hash',
    state: 'validation_passed', summary: {generated_rows: 2}};
  const accepted = {...batch, state: 'gaia_accepted', gaia_receipt: {
    execution_key: 'key', phase: 'accepted', message: '盖亚已受理，未启动抖店发布'}};
  const defaults = {
    '/api/gaia/capabilities': {success: true, features: {gaia_submit: true}, contracts: {
      mapping_policy: 'explicit_mainland_only_v2', payment_terms_policy: 'explicit_terms_v1'}},
    '/api/gaia/qualities/status': {success: true, qualities: {
      '99新': {configured: true}, '95新': {configured: true},
      '9新': {configured: true}, '准新机': {configured: true}}},
    '/api/gaia/model-mappings/latest': {success: true, mapping},
    '/api/gaia/templates/current': {success: true, template},
    '/api/gaia/templates': {success: true, template},
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
  elements['#gaiaActivityName'].value = '【专享】年框商家双11大回馈';
  elements['#gaiaActivityName'].focus = () => {};
  return {elements, handlers, calls, defaults, timers,
    start: () => handlers['#gaiaStartBtn'].click(),
    count: suffix => calls.filter(call => call.url.endsWith(suffix)).length};
}

test('one click automatically loads, matches, generates, verifies, starts and polls', async () => {
  const f = await fixture();
  assert.equal(f.count('/start'), 0, 'loading the page must never submit');
  assert.match(f.elements['#gaiaInputVersions'].textContent, /模板 cccccccc.*映射表 aaaaaaaa/);
  await f.start();
  assert.equal(f.count('/excel/latest'), 0, 'normal workflow must not scan Downloads');
  assert.equal(f.count('/templates/current'), 2, 'initial display and run both verify the current template');
  for (const endpoint of ['/resolve-batch', '/preview', '/generate', '/start']) assert.equal(f.count(endpoint), 1);
  const request = f.calls.find(call => call.url.endsWith('/generate'));
  const resolveRequest = f.calls.find(call => call.url.endsWith('/resolve-batch'));
  assert.deepEqual(JSON.parse(resolveRequest.options.body).quality_labels, ['99新']);
  assert.deepEqual(JSON.parse(request.options.body).selected_sku_ids, ['sku1', 'sku2']);
  assert.deepEqual(JSON.parse(request.options.body).quality_labels, ['99新']);
  assert.equal(JSON.parse(request.options.body).quality_link, '1');
  assert.equal(JSON.parse(request.options.body).expected_quality_link, '1');
  assert.deepEqual(JSON.parse(request.options.body).expected_quality_ids, ['99']);
  assert.match(f.elements['#gaiaSelectedMappingFile'].textContent, /版本 aaaaaaaa/);
  assert.equal(f.elements['#gaiaGenerationStatus'].textContent, '盖亚商品已核实');
  assert.match(f.elements['#gaiaUploadStatus'].textContent, /未启动抖店发布/);
  assert.equal(f.elements['#gaiaFormFields'].disabled, false);
  assert.equal(f.elements['#gaiaStartBtn'].disabled, true);
  assert.equal(f.elements['#gaiaStopBtn'].textContent, '已提交，无法撤回');
  assert.equal(f.elements['#gaiaStopBtn'].disabled, true);
  assert.match(f.elements['#gaiaStopHint'].textContent, /本地停止不能撤回/);
  await f.start();
  assert.equal(f.count('/start'), 1, 'completed batch cannot be submitted twice');
});

test('selecting no quality link sends 0 through preview and generation', async () => {
  const f = await fixture((url, _options, defaults) =>
    url.endsWith('/preview') ? {success: true, preview: {...defaults[url].preview, quality_link: '0'}} : undefined);
  f.elements['#gaiaQualityLink'].value = '0';
  f.handlers['#gaiaQualityLink'].change();
  await f.start();
  const previewCall = f.calls.find(call => call.url.endsWith('/preview'));
  const generateCall = f.calls.find(call => call.url.endsWith('/generate'));
  assert.equal(JSON.parse(previewCall.options.body).quality_link, '0');
  assert.equal(JSON.parse(generateCall.options.body).quality_link, '0');
  assert.equal(JSON.parse(generateCall.options.body).expected_quality_link, '0');
});

test('one click sends only safely matched models and keeps skipped rows visible', async () => {
  const f = await fixture((url, _options, defaults) => url.endsWith('/resolve-batch')
    ? {success: true, partial: true, selected_sku_ids: ['sku1'], results: [
      {index: 1, brand_input: '苹果', model_input: 'iPhone 17 Pro', unique_model: true,
        candidate_count: 1, excluded_sku_ids: ['bad-sku'],
        candidates: [{brand: '苹果', model: 'iPhone 17 Pro'}]},
      {index: 2, brand_input: '苹果', model_input: 'iPhone 18 Pro', unique_model: false,
        candidate_count: 0, candidates: []},
    ], message: '已匹配 1 个型号，跳过 1 个未找到的型号；本批仅填写 1 个国行标品 ID'}
    : defaults[url]);
  f.elements['#gaiaModels'].value = '苹果,iPhone 17 Pro\n苹果,iPhone 18 Pro';
  await f.start();
  assert.equal(f.count('/start'), 1);
  const request = f.calls.find(call => call.url.endsWith('/generate'));
  assert.deepEqual(JSON.parse(request.options.body).selected_sku_ids, ['sku1']);
  assert.equal(JSON.parse(request.options.body).allow_partial, true);
  assert.match(f.elements['#gaiaModelMatchResult'].textContent, /✕ 第 2 行.*iPhone 18 Pro/);
  assert.match(f.elements['#gaiaModelMatchResult'].textContent, /排除盖亚此前拒绝的 ID：bad-sku/);
  assert.match(f.elements['#gaiaModelMatchResult'].textContent, /跳过 1 个/);
});

test('全款一键发品不进入价格规则预检或导入', async () => {
  const f = await fixture();
  f.elements['#gaiaPaymentOptions'].choose('full');
  await f.start();
  const start = f.calls.find(call => call.url.endsWith('/start'));
  assert.deepEqual(JSON.parse(start.options.body).installment_terms, []);
  assert.equal(f.count('/price-uplift/preview'), 0);
  assert.equal(f.count('/price-uplift/download'), 0);
  assert.equal(f.elements['#gaiaPricePreview'].disabled, true);
  assert.match(f.elements['#gaiaPriceResult'].textContent, /跳过分期规则导入/);
});

test('new payment UI refuses to submit against a server without the new contract', async () => {
  const f = await fixture((url, _options, defaults) => url === '/api/gaia/capabilities'
    ? {success: true, features: {gaia_submit: true},
      contracts: {mapping_policy: 'explicit_mainland_only_v2'}} : defaults[url]);
  assert.equal(f.elements['#gaiaStartBtn'].disabled, true);
  await f.start();
  assert.equal(f.count('/start'), 0);
});

test('activity name is isolated by shop and term, hidden for full payment', async () => {
  const f = await fixture();
  f.handlers['#gaiaShop'].change();
  assert.equal(f.elements['#gaiaActivityField'].hidden, false);
  f.elements['#gaiaActivityName'].value = '【专享】年框商家双11大回馈';
  f.handlers['#gaiaActivityName'].input();
  f.elements['#gaiaPaymentOptions'].choose('24');
  assert.equal(f.elements['#gaiaActivityName'].value, '');
  f.elements['#gaiaPaymentOptions'].options.find(option => option.value === '24').checked = false;
  f.handlers['#gaiaPaymentOptions'].change({target: {value: '24', checked: false}});
  assert.equal(f.elements['#gaiaActivityName'].value, '【专享】年框商家双11大回馈');
  f.elements['#gaiaPaymentOptions'].choose('full');
  assert.equal(f.elements['#gaiaActivityField'].hidden, true);
  assert.equal(f.elements['#gaiaActivityName'].disabled, true);
});

test('failed pre-submit price stage offers price-only recovery without recreating goods', async () => {
  const productId = '3847478441691709635';
  const receipt = {execution_key: 'key', phase: 'accepted', submit_intent: true,
    price_phase: 'failed', price_installments: 12,
    price_resume_started: true, price_resume_attempts: 1,
    result: {outcome: 'accepted', verified_product_ids: [productId]}};
  const failed = {id: 'abc123', shop_id: '7970406', output_hash: 'output-hash',
    state: 'manual_required', summary: {generated_rows: 2, expected_product_count: 1},
    doudian_draft_ids: [productId], gaia_receipt: receipt};
  const recovered = {...failed, state: 'handed_off', gaia_receipt: {...receipt,
    price_phase: 'accepted', price_resume_started: true}};
  let resumed = false;
  const f = await fixture((url) => {
    if (url.endsWith('/start')) return {success: true, batch: failed};
    if (url.endsWith('/price-uplift/resume')) {
      resumed = true;
      return {success: true, batch: recovered};
    }
    if (url.endsWith('/batches/abc123')) return {success: true, batch: resumed ? recovered : failed};
  });
  await f.start();
  assert.equal(f.elements['#gaiaPriceResume'].hidden, false);
  assert.equal(f.elements['#gaiaPriceResume'].disabled, false);
  await f.elements['#gaiaPriceResume'].click();
  assert.equal(f.count('/start'), 1);
  assert.equal(f.count('/price-uplift/resume'), 1);
  assert.equal(f.elements['#gaiaPriceResume'].hidden, true);
});

test('all four configured qualities are sent together and unknown codes block start', async () => {
  const f = await fixture((url, options, defaults) => {
    if (url === '/api/gaia/batches/preview' || url === '/api/gaia/batches/generate') {
      const qualities = JSON.parse(options.body).quality_labels;
      const preview = {...defaults['/api/gaia/batches/preview'].preview,
        row_count: 2 * qualities.length, quality_labels: qualities,
        quality_ids: qualities.map(label => ({'99新': '99', '95新': '95', '9新': '90', '准新机': '100'})[label])};
      return url.endsWith('/generate')
        ? {...defaults[url], preview, batch: {...defaults[url].batch,
          summary: {generated_rows: preview.row_count}}}
        : {success: true, preview};
    }
  });
  f.elements['#gaiaCondition'].selectedValues = ['99新', '95新', '9新', '准新机'];
  f.handlers['#gaiaCondition'].change();
  await f.start();
  const generated = JSON.parse(f.calls.find(call => call.url.endsWith('/generate')).options.body);
  assert.deepEqual(generated.quality_labels, ['99新', '95新', '9新', '准新机']);
  assert.deepEqual(generated.expected_quality_ids, ['99', '95', '90', '100']);

  const blocked = await fixture(url => url === '/api/gaia/qualities/status'
    ? {success: true, qualities: {'99新': {configured: true}, '9新': {configured: false, message: '9新编码未确认'}}}
    : undefined);
  blocked.elements['#gaiaCondition'].selectedValues = ['99新', '9新'];
  blocked.handlers['#gaiaCondition'].change();
  assert.equal(blocked.elements['#gaiaStartBtn'].disabled, true);
  assert.match(blocked.elements['#gaiaGeneratedPreview'].textContent, /9新编码未确认/);
  assert.equal(blocked.count('/start'), 0);
});

test('an invalid template update does not replace the managed version or start a run', async () => {
  const f = await fixture(url => url === '/api/gaia/templates'
    ? {success: false, message: '模板表头不匹配'} : undefined);
  f.elements['#gaiaTemplateFile'].files = [{name: 'wrong.xlsx', size: 2048}];
  await f.handlers['#gaiaTemplateFile'].change();
  assert.equal(f.count('/templates'), 1);
  assert.match(f.elements['#gaiaSelectedFile'].textContent, /版本 cccccccc/);
  assert.equal(f.count('/start'), 0);
  assert.equal(f.count('/excel/latest'), 0);
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

for (const stage of ['/templates/current', '/model-mappings/latest', '/resolve-batch', '/preview', '/generate', '/handoff-preflight?shop_id=7970406']) {
  test('Stop during preparation never starts browser: ' + stage, async () => {
    let release, cancelled, pending = false, stageCalls = 0;
    const wait = new Promise(resolve => {release = resolve;});
    const f = await fixture(async (url, options, defaults) => {
      if (url.endsWith(stage) && ++stageCalls === (stage === '/templates/current' || stage === '/model-mappings/latest' ? 2 : 1)) {
        pending = true; await wait;
      }
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

for (const stage of ['/templates/current', '/model-mappings/latest', '/resolve-batch', '/preview', '/generate', '/handoff-preflight?shop_id=7970406']) {
  test('failure stops before upload and persists its reason: ' + stage, async () => {
    let stageCalls = 0;
    const f = await fixture(url => url.endsWith(stage)
      && ++stageCalls === (stage === '/templates/current' || stage === '/model-mappings/latest' ? 2 : 1)
      ? {success: false, message: '校验不通过：测试原因'} : undefined);
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
