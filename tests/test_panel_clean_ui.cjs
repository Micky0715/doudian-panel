/* Offline UI fixtures: no browser, network, publishing or real task writes. */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'panel.html'), 'utf8');
const script = fs.readFileSync(path.join(root, 'assets/panel.js'), 'utf8');

function makeElement(attrs = '') {
  const attr = name => attrs.match(new RegExp('(?:^|\\s)' + name + '="([^"]*)"'))?.[1] || '';
  const classes = new Set(attr('class').split(/\s+/));
  const events = {};
  const el = {id: attr('id'), value: attr('value'), textContent: '', style: {}, events,
    disabled: /\sdisabled(?:\s|$)/.test(attrs), checked: /\schecked(?:\s|$)/.test(attrs),
    dataset: Object.fromEntries([...attrs.matchAll(/data-([\w-]+)="([^"]*)"/g)].map(([, key, value]) =>
      [key.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase()), value])),
    classList: {contains: key => classes.has(key), add: key => classes.add(key), remove: key => classes.delete(key),
      toggle(key, value) {value ? classes.add(key) : classes.delete(key);}},
    addEventListener(name, fn) {events[name] = fn;},
    click() {return (events.click || el.onclick)?.();},
    appendChild() {}, querySelector() {return null;}, querySelectorAll() {return [];},
  };
  return el;
}

async function fixture() {
  const nodes = [...html.matchAll(/<[a-z][\w-]*\b([^>]*)>/gi)].map(([, attrs]) => makeElement(attrs));
  const elements = new Map();
  for (const el of nodes.filter(el => el.id)) {
    assert.equal(elements.has(el.id), false, 'duplicate ID: ' + el.id);
    elements.set(el.id, el);
  }
  const tabs = nodes.filter(el => el.classList.contains('workspace-tab'));
  const views = nodes.filter(el => el.dataset.viewPanel);
  const segments = nodes.filter(el => el.classList.contains('seg'));
  const get = id => elements.get(id) || null;
  get('modeSeg').querySelectorAll = () => segments;
  get('modeSeg').querySelector = () => segments.find(el => el.classList.contains('active'));
  get('allErrBtn').querySelector = () => nodes.find(el => el.classList.contains('ae-count'));
  // A select's value is its selected (otherwise first) option in the HTML.
  for (const [, id, options] of html.matchAll(/<select[^>]*\bid="([^"]+)"[^>]*>([\s\S]*?)<\/select>/g)) {
    get(id).value = options.match(/<option value="([^"]*)"[^>]*\bselected/)?.[1]
      ?? options.match(/<option value="([^"]*)"/)?.[1] ?? '';
  }
  const requests = [], intervals = [], streams = [], store = new Map();
  let gaiaInitialized = false;
  const document = {
    getElementById: get, addEventListener() {}, createElement: () => makeElement(),
    querySelector(selector) {
      if (selector.startsWith('#')) return get(selector.slice(1));
      const view = selector.match(/\.workspace-tab\[data-view="([^"]+)"\]/)?.[1];
      return view ? tabs.find(tab => tab.dataset.view === view) : null;
    },
    querySelectorAll(selector) {
      if (selector === '.workspace-tab') return tabs;
      if (selector === '[data-view-panel]') return views;
      return [];
    },
  };
  vm.runInNewContext(script, {
    document, window: {addEventListener() {}, initGaiaWorkspace() {gaiaInitialized = true;}},
    localStorage: {getItem: key => store.get(key), setItem: (key, value) => store.set(key, value)},
    setInterval: (fn, ms) => intervals.push({fn, ms}), setTimeout() {}, clearTimeout() {}, confirm: () => true,
    EventSource: class {constructor(url) {this.url = url; streams.push(this);}},
    fetch: async (url, options) => {
      requests.push({url, options});
      let data;
      if (url === '/api/shops') data = {shops: [{id: '7970406', name: '测试店铺'}], max_parallel: 4};
      else if (url === '/api/tasks') data = {tasks: []};
      else if (url === '/api/run') data = {success: true, message: '仅离线模拟，不启动任务'};
      else assert.fail('unexpected request after removing secondary panels: ' + url);
      return {json: async () => data};
    },
  });
  await new Promise(resolve => setImmediate(resolve));
  return {get, requests, intervals, streams, tabs, views, segments, gaiaInitialized};
}

test('secondary UI is removed, while backend endpoints are not deleted', () => {
  for (const text of ['素材图库', '标准型号库', 'API 型号体检', '访问地址与开机自启']) {
    assert.ok(!html.includes(text), text);
  }
  for (const id of ['catalogueBtn', 'catalogueQuery', 'auditModelBtn', 'autoBtn', 'netList']) {
    assert.ok(!html.includes('id="' + id + '"'));
    assert.ok(!script.includes("$('#" + id + "')"));
  }
  const server = fs.readFileSync(path.join(root, 'panel_server.py'), 'utf8');
  for (const route of ['/album', '/api/model-audit', '/api/model-catalogue', '/api/network']) {
    assert.ok(server.includes('"' + route + '"'), route);
  }
});

test('advanced controls have unique IDs and are collapsed by default', () => {
  const advanced = html.match(/<details[^>]*id="doudianAdvanced"[^>]*>([\s\S]*?)<\/details>/);
  assert.ok(advanced);
  assert.doesNotMatch(advanced[0].split('>')[0], /\bopen\b/);
  for (const id of ['engineSel', 'releaseStage', 'productId', 'smartRunBtn']) {
    assert.ok(advanced[1].includes('id="' + id + '"'), id);
    assert.equal(html.split('id="' + id + '"').length - 1, 1);
  }
  assert.match(html, /id="saveOnly" checked/);
  assert.match(html, /id="gaiaStopBtn"/);
});

test('panel initializes without removed elements, and keeps task synchronization', async () => {
  const f = await fixture();
  assert.equal(f.gaiaInitialized, true);
  assert.equal(f.streams[0].url, '/api/stream');
  assert.equal(f.intervals.length, 1, 'network address polling is removed');
  assert.equal(f.intervals[0].ms, 3000);
  assert.ok(f.requests.every(r => !r.options?.method), 'page load never submits');
  f.streams[0].onopen();
  assert.equal(f.get('connTxt').textContent, '已连接');
  assert.equal(f.get('slotBadge').textContent, '并行 0 / 4');
  await f.intervals[0].fn();
  assert.equal(f.requests.filter(r => r.url === '/api/tasks').length, 2);
  assert.equal(typeof f.get('allErrBtn').onclick, 'function');
});

test('both workspace tabs remain operable', async () => {
  const f = await fixture();
  for (const view of ['doudian', 'gaia']) {
    f.tabs.find(tab => tab.dataset.view === view).click();
    assert.deepEqual(f.views.filter(el => el.classList.contains('active')).map(el => el.dataset.viewPanel), [view]);
  }
});

for (const mode of ['test', 'batch']) {
  test('main Run keeps the existing request contract in ' + mode + ' mode', async () => {
    const f = await fixture();
    f.get('shopSel').value = '7970406';
    f.segments.find(el => el.dataset.mode === mode).click();
    await f.get('runBtn').click();
    const request = f.requests.find(r => r.url === '/api/run');
    assert.deepEqual(JSON.parse(request.options.body), {
      shop_id: '7970406', engine: 'api_media', mode, test_n: 1,
      release_stage: '', release_stage_explicit: false, product_id: '', save_only: true,
    });
  });
}

test('browser fallback and one-item action still work from advanced settings', async () => {
  const f = await fixture();
  f.get('shopSel').value = '7970406';
  f.get('engineSel').value = 'browser';
  f.get('productId').value = '123456';
  await f.get('smartRunBtn').click();
  assert.deepEqual(JSON.parse(f.requests.find(r => r.url === '/api/run').options.body), {
    shop_id: '7970406', engine: 'browser', mode: 'test', test_n: 1, product_id: '123456', save_only: true,
  });
});
