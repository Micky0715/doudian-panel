/* Offline DOM-adapter fixtures. No browser launch, network or file upload. */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../integrations/gaia_browser/file_attachment.py'), 'utf8');
const script = source.match(/ATTACHMENT_SNAPSHOT_JS = r"""([\s\S]*?)"""/)[1];
const collect = vm.runInNewContext('(' + script + ')', {
  getComputedStyle: el => el.style || {display: 'block', visibility: 'visible', opacity: '1'},
});
const name = 'gaia_upload.xlsx';

function element({text = '', classes = '', attributes = {}, hidden = false, nodes = {}, parent = null} = {}) {
  return {isConnected: true, nodeType: 1, parentElement: parent,
    textContent: text, innerText: text, classList: {contains: value => classes.split(' ').includes(value)},
    getClientRects: () => hidden ? [] : [{}],
    getAttribute: key => attributes[key] || null,
    querySelectorAll: selector => nodes[selector] || [],
  };
}
function row({state = 'done', filename = name, hidden = false, progress = false, title = filename, explicit = '', elementUI = false} = {}) {
  const label = element({text: filename, attributes: {title}});
  return element({text: filename, hidden,
    classes: elementUI ? 'el-upload-list__item is-' + state : 'ant-upload-list-item ant-upload-list-item-' + state,
    attributes: {'data-status': explicit}, nodes: {
      '.ant-upload-list-item-name, .el-upload-list__item-name': [label],
      '[role="progressbar"], .ant-upload-list-item-progress, .el-progress': progress ? [element()] : [],
    }});
}
function snapshot(rows, files = []) {
  const root = element({text: '下载模板 ' + name, nodes: {
    '.ant-upload-list-item, .el-upload-list__item': rows,
    'input[type="file"]': [{files}],
  }});
  return collect(root);
}

test('Ant list retains exact file evidence when native input has been reset', () => {
  const data = snapshot([row()]);
  assert.equal(data.inputs.length, 0);
  assert.equal(data.entries[0].names[0].text, name);
  assert.equal(data.entries[0].state, 'done');
});
test('hidden stale uploads and unrelated help text are not attachment evidence', () => {
  assert.equal(snapshot([row({hidden: true})]).entries.length, 0);
  assert.equal(snapshot([]).entries.length, 0);
});
test('error, uploading and progress remain blocking states', () => {
  assert.equal(snapshot([row({state: 'error'})]).entries[0].state, 'error');
  assert.equal(snapshot([row({state: 'removed'})]).entries[0].state, 'error');
  assert.equal(snapshot([row({state: 'uploading'})]).entries[0].state, 'uploading');
  assert.equal(snapshot([row({progress: true})]).entries[0].state, 'uploading');
});
test('unknown explicit statuses are not treated as done', () => {
  assert.equal(snapshot([row({explicit: 'paused'})]).entries[0].state, 'unknown');
});
test('Element success, failure and queued files are distinguished', () => {
  assert.equal(snapshot([row({elementUI: true, state: 'success'})]).entries[0].state, 'done');
  assert.equal(snapshot([row({elementUI: true, state: 'fail'})]).entries[0].state, 'error');
  assert.equal(snapshot([row({elementUI: true, state: 'ready'})]).entries[0].state, 'uploading');
});
test('duplicate entries and conflicting native selections remain visible to validator', () => {
  const data = snapshot([row(), row()], [{name: 'wrong.xlsx', size: 999}]);
  assert.equal(data.entries.length, 2);
  assert.equal(data.inputs[0].name, 'wrong.xlsx');
  assert.equal(data.inputs[0].size, 999);
});
