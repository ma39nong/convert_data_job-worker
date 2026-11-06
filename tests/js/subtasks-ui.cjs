const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const script=fs.readFileSync('src/convest/subtasks/static/app.js','utf8');
vm.runInThisContext(script.slice(script.indexOf('function synchronizeFine('),script.indexOf('// End pure boundary projection.')));
const mapping={grasp_truss:'pickup',lift_truss:'pickup',grasp_fruit:'harvest',pick_and_place:'harvest',return_truss:'return'};
const fine=[['grasp_truss',0,10],['lift_truss',10,30],['grasp_fruit',30,40],['pick_and_place',40,70],['return_truss',70,100]].map(([label,start_frame,end_frame])=>({label,start_frame,end_frame,reviewed:true}));
const coarse=[['pickup',0,30],['harvest',30,70],['return',70,100]].map(([label,start_frame,end_frame])=>({label,start_frame,end_frame}));
const baseline=JSON.stringify(fine);
assert.deepEqual(synchronizeFine(coarse,fine,mapping),fine);
coarse[0].end_frame=25;coarse[1].start_frame=25;
let out=synchronizeFine(coarse,fine,mapping);
assert.deepEqual(out.map(s=>[s.start_frame,s.end_frame]),[[0,10],[10,25],[25,40],[40,70],[70,100]]);
assert.deepEqual(out.map(s=>s.reviewed),[true,false,false,true,true]);
assert.equal(JSON.stringify(fine),baseline);
// A large move must remove swallowed cuts, never produce empty/reversed spans.
coarse[0].end_frame=45;coarse[1].start_frame=45;
out=synchronizeFine(coarse,fine,mapping);
assert.deepEqual(out.map(s=>[s.start_frame,s.end_frame]),[[0,10],[10,45],[45,70],[70,100]]);
assert.equal(out[2].label,'pick_and_place');
assert.deepEqual(synchronizeFine(coarse,out,mapping),out);
coarse[1].label='adjust';out=synchronizeFine(coarse,fine,mapping);
assert(out.filter(s=>s.start_frame>=45&&s.end_frame<=70).every(s=>s.label==='unknown'&&!s.reviewed));
// Regression: selecting another segment commits the old editor exactly once.
const context={state:{mode:'coarse',selected:{coarse:0},episode:{length:100,fps:30},videos:[]},requireEpisode(){},pause(){},updateTime(){},renderEditor(){},renderTracks(){},commits:0,activeRows(){return [{start_frame:0},{start_frame:30}];},commitFields(){this.commits++;}};
// Count lexical calls rather than relying on this binding.
context.commitFields=()=>context.commits++;
vm.createContext(context);
for(const name of ['seek','select']){const line=script.split('\n').find(l=>l.startsWith('function '+name+'('));vm.runInContext(line,context);}
vm.runInContext('select(1)',context);assert.equal(context.commits,1);assert.equal(context.state.selected.coarse,1);
console.log('subtasks UI: boundary sync and navigation regression passed');
