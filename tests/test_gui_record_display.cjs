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

test('standard local scene and A/B selections infer the scene without changing manual targets', () => {
  const context = vm.createContext({});
  const source = fs.readFileSync(path.join(__dirname, '../src/convest/gui/static/app.js'), 'utf8');
  vm.runInContext(source.replace(/init\(\);\s*$/, ''), context);
  vm.runInContext(`
    var task = {source:'/bags/no-block-one/B',scene:'old',project:'tomato',robot:'sharpa',auto_upload_paths:true};
    inferSourceScene(task);
  `, context);
  assert.equal(vm.runInContext('task.scene', context), 'no-block-one');
  assert.equal(vm.runInContext('task.raw_root', context), '/data/data-demo/real/tomato/data/rosbag/sharpa/no-block-one');
  vm.runInContext(`task.auto_upload_paths=false; task.source='/bags/full-block-more/'; inferSourceScene(task);`, context);
  assert.equal(vm.runInContext('task.scene', context), 'full-block-more');
  assert.equal(vm.runInContext('task.raw_root', context), '/data/data-demo/real/tomato/data/rosbag/sharpa/no-block-one');
  vm.runInContext(`task.source='/bags/unknown/A'; inferSourceScene(task);`, context);
  assert.equal(vm.runInContext('task.scene', context), 'full-block-more');
});

test('OSS preview maps final paths, escapes values and does not offer deletion controls', () => {
  const elements = new Map();
  const context = vm.createContext({document:{getElementById(id) {
    if (!elements.has(id)) elements.set(id, {});
    return elements.get(id);
  }}});
  const source = fs.readFileSync(path.join(__dirname, '../src/convest/gui/static/app.js'), 'utf8');
  vm.runInContext(source.replace(/init\(\);\s*$/, ''), context);
  vm.runInContext(`draft={training_delivery:{mode:'oss',transit_root:'/oss',final_root:'/datasets'},tasks:[{id:'a',normal_root:'/datasets/tomato_sharpa/normal/no-block-one',speedup_root:'/datasets/tomato_sharpa/speedup/<scene>'}]}; selected='a'; renderTransitPreview();`,context);
  const html=elements.get('oss-transit-content').innerHTML;
  assert.match(html,/\/oss\/tomato_sharpa\/normal\/no-block-one\/\{A,B\}/);
  assert.match(html,/&lt;scene&gt;/);
  assert.doesNotMatch(html,/<button|<input/);
  assert.match(elements.get('training-delivery-hint').textContent,/看不到该批文件是正常/);
  vm.runInContext(`draft.training_delivery.mode='direct';renderTransitPreview();`,context);
  assert.match(elements.get('training-delivery-hint').textContent,/未启用/);
});

test('OSS stage chain highlights real failure and confirms cleanup only on completion', () => {
  const context=vm.createContext({});
  const source=fs.readFileSync(path.join(__dirname,'../src/convest/gui/static/app.js'),'utf8');
  vm.runInContext(source.replace(/init\(\);\s*$/, ''),context);
  const retry=vm.runInContext(`transferCard('normal',{phase:'retry',failed_phase:'oss_cleanup',error:'access denied',transit:'/oss/batch'})`,context);
  assert.match(retry,/stage-error">清理/);
  assert.match(retry,/清理中转失败，等待重试/);
  assert.doesNotMatch(retry,/中转已清理/);
  const done=vm.runInContext(`transferCard('normal',{phase:'completed',transit_cleaned:true,transit:'/oss/batch'})`,context);
  assert.match(done,/最终数据已校验 · 中转已清理/);
});
