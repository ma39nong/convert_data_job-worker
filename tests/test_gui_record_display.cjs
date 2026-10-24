const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

test('clearing masks existing rows across refresh and task switches, then shows new rows', () => {
  const elements = new Map();
  const context = vm.createContext({ document: { getElementById(id) {
    if (!elements.has(id)) elements.set(id, {
      classList: { toggle() {} }, getAttribute: () => 'true',
    });
    return elements.get(id);
  } } });
  const source = fs.readFileSync(path.join(__dirname, '../src/convest/gui/static/app.js'), 'utf8');
  vm.runInContext(source.replace(/init\(\);\s*$/, ''), context);
  vm.runInContext(`
    draft = {tasks: [{id:'a', name:'A', enabled:true}, {id:'b', name:'B', enabled:true}]};
    selected = 'a';
    var fixture = {state:'stopped', jobs:[{route:'A', episode:'old-episode', state:'converted'}],
      batches:[{route:'A', batch_id:'old-batch', name:'old-batch', state:'uploaded'}],
      training:[{route:'A', id:'old-training', state:'uploaded'}]};
    snapshot = {tasks:[], updated_ns:0};
    state = () => fixture;
    var before = JSON.stringify(fixture);
    renderProgress();
  `, context);
  assert.match(elements.get('records').innerHTML, /old-episode/);
  vm.runInContext('clearRecordDisplay(); renderProgress();', context);
  assert.match(elements.get('records').innerHTML, /显示已清空/);
  assert.equal(vm.runInContext('JSON.stringify(fixture) === before', context), true);
  vm.runInContext("selected = 'b'; renderProgress();", context);
  assert.match(elements.get('records').innerHTML, /old-episode/);
  vm.runInContext("selected = 'a'; fixture.jobs[0].state = 'retry'; renderProgress();", context);
  assert.doesNotMatch(elements.get('records').innerHTML, /old-episode/);
  vm.runInContext(`
    fixture.jobs.push({route:'A',episode:'new-episode',state:'pending'});
    fixture.batches.push({route:'A',batch_id:'new-batch',name:'new-batch',state:'active'});
    fixture.training.push({route:'A',id:'new-training',state:'pending'});
    renderProgress();
  `, context);
  assert.match(elements.get('records').innerHTML, /new-episode/);
  assert.match(elements.get('records').innerHTML, /new-batch/);
  assert.match(elements.get('records').innerHTML, /new-training/);
  assert.doesNotMatch(elements.get('records').innerHTML, /old-episode|old-batch|old-training/);
});
