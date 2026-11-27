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

test('batch footer counts A+B valid sources and no longer shows a 24-hour countdown', () => {
  const elements=new Map();
  const context=vm.createContext({document:{getElementById(id) {
    if(!elements.has(id))elements.set(id,{classList:{toggle(){}},getAttribute:()=> 'true'});
    return elements.get(id);
  }}});
  const source=fs.readFileSync(path.join(__dirname,'../src/convest/gui/static/app.js'),'utf8');
  vm.runInContext(source.replace(/init\(\);\s*$/, ''),context);
  vm.runInContext(`draft={tasks:[{id:'a',name:'A',enabled:true}]};selected='a';snapshot={tasks:[],updated_ns:0};
    state=()=>({state:'stopped',jobs:[],uploads:[],training:[],batches:[
      {route:'sharpa-A',batch_id:'batch-1',state:'active',sources:JSON.stringify(Array.from({length:60},(_,i)=>'episode'+i))},
      {route:'sharpa-B',batch_id:'batch-2',state:'active',sources:JSON.stringify(Array.from({length:39},(_,i)=>'episode'+i))}
    ]});renderProgress();`,context);
  assert.match(elements.get('batch-clock').textContent,/99\/100/);
  assert.match(elements.get('batch-clock').textContent,/还差 1 条/);
  assert.doesNotMatch(elements.get('transfer-grid').innerHTML,/24h/);
});

test('raw preview includes C/D while conversion stays A/B and raw backup counts deduplicate', () => {
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
    draft = {tasks:[{id:'a',name:'A',source:'/bags/scene',raw_root:'/raw',lerobot_root:'/full',normal_root:'/normal',speedup_root:'/speedup'}]};
    selected='a'; renderTransitPreview=()=>{}; renderPreviews();
    snapshot={tasks:[],updated_ns:0};
    state=()=>({state:'stopped',jobs:[],batches:[],training:[],
      uploads:[{route:'sharpa-A',episode:'episode1',raw_done:1}],
      raw_archives:[{route:'sharpa-A',episode:'episode1',state:'uploaded'},
                    {route:'sharpa-C',episode:'episode2',state:'deleted'},
                    {route:'sharpa-D',episode:'episode3',state:'uploaded'}]});
    renderProgress();
  `, context);
  assert.match(elements.get('raw-preview').innerHTML, /C \/ episode/);
  assert.match(elements.get('raw-preview').innerHTML, /D \/ episode/);
  for (const id of ['data-preview','normal-preview','train-preview']) {
    assert.doesNotMatch(elements.get(id).innerHTML, /[CD] \/ /);
  }
  assert.match(elements.get('metrics').innerHTML, /202 已备份原包<\/div><div class="value">3<\/div>/);
  assert.match(elements.get('records').innerHTML, /仅备份，不转换/);
});

test('manual upload requires confirmation, sends the selected task and blocks double clicks', async () => {
  const elements=new Map();
  let confirmed=false, requests=0, release;
  const context=vm.createContext({
    document:{getElementById(id) {
      if(!elements.has(id))elements.set(id,{classList:{toggle(){}},getAttribute:()=> 'true'});
      return elements.get(id);
    }},
    window:{confirm:()=>confirmed},
    fetch:async (url,options)=>{
      requests++;
      assert.equal(url,'/api/upload-now');
      assert.deepEqual(JSON.parse(options.body),{task:'a'});
      await new Promise(resolve=>{release=resolve;});
      return {ok:true,json:async()=>({tasks:[{id:'a',state:'running',batch_policy:{size:25},batches:[]}],updated_ns:0})};
    },
  });
  const source=fs.readFileSync(path.join(__dirname,'../src/convest/gui/static/app.js'),'utf8');
  vm.runInContext(source.replace(/init\(\);\s*$/, ''),context);
  vm.runInContext(`toast=()=>{};draft={tasks:[{id:'a',enabled:true,batch_group_size:50}]};selected='a';
    snapshot={updated_ns:0,tasks:[{id:'a',state:'running',batch_policy:{size:25},batches:[{route:'A',state:'active',sources:'["episode1"]'}]}]};renderProgress();`,context);
  assert.match(elements.get('batch-clock').textContent,/1\/25/); // active policy, not unsaved input
  await vm.runInContext('uploadNow()',context);
  assert.equal(requests,0);
  confirmed=true;
  const pending=vm.runInContext('uploadNow()',context);
  assert.equal(elements.get('upload-now').disabled,true);
  await vm.runInContext('uploadNow()',context);
  assert.equal(requests,1);
  release();
  await pending;
  assert.equal(elements.get('upload-now').disabled,true); // no pending recordings
});

test('recovered queue state clears stale telemetry errors while genuine retries stay visible', () => {
  const elements=new Map();
  const context=vm.createContext({document:{getElementById(id) {
    if(!elements.has(id))elements.set(id,{classList:{toggle(){}},getAttribute:()=> 'true'});
    return elements.get(id);
  }}});
  const source=fs.readFileSync(path.join(__dirname,'../src/convest/gui/static/app.js'),'utf8');
  vm.runInContext(source.replace(/init\(\);\s*$/, ''),context);
  vm.runInContext(`draft={tasks:[{id:'a',enabled:true}]};selected='a';snapshot={tasks:[],updated_ns:0};
    var fixture={state:'running',jobs:[{route:'A',episode:'one',state:'converted',error:'old conversion'}],
      uploads:[{route:'A',episode:'one',state:'completed',error:'old network'}],
      training:[{id:'train',state:'uploaded',error:'old upload'}],
      raw_upload:{route:'A',episode:'one',phase:'retry',error:'stale raw'},
      normal_upload:{task:'train',phase:'retry',error:'stale normal'},
      training_upload:{task:'train',phase:'retry',error:'stale overall'},
      training_processing:{task:'train',phase:'retry',error:'stale processing'}};
    state=()=>fixture;renderProgress();`,context);
  assert.equal(elements.get('workflow-error').textContent,'');
  assert.doesNotMatch(elements.get('transfer-grid').innerHTML,/stale/);
  assert.doesNotMatch(elements.get('records').innerHTML,/old network|old upload|old conversion/);
  vm.runInContext(`fixture.uploads[0].state='retry';fixture.uploads[0].error='still disconnected';renderProgress();`,context);
  assert.match(elements.get('workflow-error').textContent,/1 项上传等待自动重试/);
  assert.match(elements.get('workflow-error').textContent,/still disconnected/);
  vm.runInContext(`fixture.uploads[0].state='uploading';fixture.uploads[0].error=null;renderProgress();`,context);
  assert.equal(elements.get('workflow-error').textContent,'');
  assert.doesNotMatch(elements.get('transfer-grid').innerHTML,/stale raw/);
});

test('retry upload button works while running, targets one task and prevents duplicate requests', async () => {
  const elements=new Map();let release,requests=0,messages=[];
  const context=vm.createContext({document:{getElementById(id) {
    if(!elements.has(id))elements.set(id,{classList:{toggle(){}},getAttribute:()=> 'true'});
    return elements.get(id);
  }},fetch:async(url,options)=>{
    requests++;assert.equal(url,'/api/retry-uploads');
    assert.deepEqual(JSON.parse(options.body),{task:'a'});
    await new Promise(resolve=>{release=resolve;});
    return {ok:true,json:async()=>({task:'a',state:'running',counts:{data:2,training:1}})};
  }});
  const source=fs.readFileSync(path.join(__dirname,'../src/convest/gui/static/app.js'),'utf8');
  vm.runInContext(source.replace(/init\(\);\s*$/, ''),context);
  context.report=(message)=>messages.push(message);
  vm.runInContext(`toast=report;draft={tasks:[{id:'a',enabled:true}]};selected='a';
    snapshot={tasks:[{id:'a',state:'running'}],updated_ns:0};renderProgress();`,context);
  assert.equal(elements.get('retry-uploads').disabled,false);
  const pending=vm.runInContext('retryUploads()',context);
  assert.equal(elements.get('retry-uploads').disabled,true);
  await vm.runInContext('retryUploads()',context);assert.equal(requests,1);
  release();await pending;
  assert.match(messages[0],/3 项失败上传/);
  assert.equal(elements.get('retry-uploads').disabled,false);
  vm.runInContext(`snapshot.tasks[0].state='stopping';renderProgress();`,context);
  assert.equal(elements.get('retry-uploads').disabled,true);
});


test('ABCD archive telemetry uses its own queue and counts independent retries', () => {
  const elements = new Map();
  const context = vm.createContext({document:{getElementById(id) {
    if (!elements.has(id)) elements.set(id,{classList:{toggle(){}},getAttribute:()=> 'true'});
    return elements.get(id);
  }}});
  const source=fs.readFileSync(path.join(__dirname,'../src/convest/gui/static/app.js'),'utf8');
  vm.runInContext(source.replace(/init\(\);\s*$/, ''),context);
  vm.runInContext(`draft={tasks:[{id:'a',enabled:true}]};selected='a';snapshot={tasks:[],updated_ns:0};
    var fixture={state:'running',uploads:[{route:'A',episode:'one',state:'completed'}],
      raw_archives:[{route:'A',episode:'one',state:'retry',error:'archive unavailable'}],
      raw_upload:{archive:true,route:'A',episode:'one',phase:'retry',error:'archive unavailable'}};
    state=()=>fixture;renderProgress();`,context);
  assert.match(elements.get('workflow-error').textContent,/1 项上传等待自动重试/);
  assert.match(elements.get('transfer-grid').innerHTML,/archive unavailable/);
  vm.runInContext(`fixture.raw_archives[0].state='uploaded';fixture.raw_archives[0].error=null;renderProgress();`,context);
  assert.equal(elements.get('workflow-error').textContent,'');
  assert.doesNotMatch(elements.get('transfer-grid').innerHTML,/archive unavailable/);
});

test('MCAP format selection changes only original archive path and keeps derived roots', () => {
  const source=fs.readFileSync(path.join(__dirname,'../src/convest/gui/static/app.js'),'utf8');
  const context=vm.createContext({});
  vm.runInContext(source.replace(/init\(\);\s*$/, ''),context);
  const roots=vm.runInContext(`var task={project:'tomato',robot:'sharpa',scene:'no-block-one',input_format:'rosbag'};
    generateUploadPaths(task);var old={...task};task.input_format='mcap';generateUploadPaths(task);({old,current:task});`,context);
  assert.match(roots.current.raw_root,/\/mcap\/sharpa\/no-block-one$/);
  assert.match(roots.old.raw_root,/\/rosbag\/sharpa\/no-block-one$/);
  for(const key of ['lerobot_root','normal_root','speedup_root'])assert.equal(roots.old[key],roots.current[key]);
});
