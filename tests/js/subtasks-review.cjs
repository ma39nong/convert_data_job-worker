const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const script=fs.readFileSync('src/convest/subtasks/static/app.js','utf8');
const elements=Object.fromEntries(['start','end','undoEdit','confirmCurrent','confirmAll','reviewSummary'].map(id=>[id,{value:'',setAttribute(k,v){this[k]=v;}}]));
const row=(label,start_frame,end_frame)=>({label,start_frame,end_frame,reviewed:true});
const stages={coarse:{note:'',revision:5,source:{id:'immutable'},segments:[row('pickup',0,30),row('harvest',30,70),row('return',70,100)]},fine:{note:'',revision:7,source:{id:'immutable'},segments:[row('grasp_truss',0,10),row('lift_truss',10,30),row('grasp_fruit',30,40),row('pick_and_place',40,70),row('return_truss',70,100)]}};
let frame=0;
const ctx={state:{stages,mode:'coarse',selected:{coarse:0,fine:0},episode:{length:100,fps:10},dirty:{}},$:(id)=>elements[id],copy:v=>JSON.parse(JSON.stringify(v)),coarseForFine:{grasp_truss:'pickup',lift_truss:'pickup',grasp_fruit:'harvest',pick_and_place:'harvest',return_truss:'return'},currentFrame:()=>frame,requireEpisode(){},pause(){},seek(f){frame=f;},message(t){ctx.lastMessage=t;},renderStatus(){ctx.renderReview();},render(){const s=ctx.state.stages[ctx.state.mode].segments[ctx.state.selected[ctx.state.mode]];elements.start.value=String(s.start_frame);elements.end.value=String(s.end_frame);ctx.renderReview();},activeRows(){return ctx.state.stages[ctx.state.mode].segments;}};
vm.createContext(ctx);
vm.runInContext(script.slice(script.indexOf('function synchronizeFine('),script.indexOf('// End pure boundary projection.')),ctx);
vm.runInContext(script.slice(script.indexOf('function syncFine('),script.indexOf('const names=')),ctx);
for(const name of ['clearReview','commitFields'])vm.runInContext(script.split('\n').find(l=>l.startsWith('function '+name+'(')),ctx);
vm.runInContext(script.slice(script.indexOf('function applyBoundaries('),script.indexOf('async function saveStage(')),ctx);
vm.runInContext('state.history=new ReviewHistory();state.savedContent=semanticContent(state.stages)',ctx);
ctx.render();const initial=JSON.stringify(stages);
elements.end.value='25';ctx.applyBoundaries();
assert.equal(stages.coarse.segments[0].end_frame,25);assert.equal(stages.fine.segments[1].end_frame,25);assert.equal(stages.fine.segments[2].start_frame,25);
assert.equal(stages.coarse.segments[2].reviewed,true);assert.equal(stages.fine.segments[0].reviewed,true);
assert(ctx.state.dirty.coarse&&ctx.state.dirty.fine);
ctx.state.mode='fine';ctx.state.selected.fine=2;frame=26;ctx.render();ctx.confirmCurrent();ctx.confirmAll();
assert(stages.fine.segments.every(s=>s.reviewed));assert.equal(stages.coarse.segments[0].reviewed,false);
assert.equal(ctx.state.history.past.length,3);
ctx.undoEdit();assert.equal(stages.fine.segments[1].reviewed,false);assert.equal(stages.fine.segments[2].reviewed,true);
ctx.undoEdit();assert.equal(stages.fine.segments[2].reviewed,false);
ctx.undoEdit();assert.equal(JSON.stringify(stages),initial);assert(!ctx.state.dirty.coarse&&!ctx.state.dirty.fine);
ctx.redoEdit();assert.equal(stages.coarse.segments[0].end_frame,25);assert.equal(stages.fine.segments[1].end_frame,25);
ctx.confirmCurrent();assert.equal(ctx.state.history.future.length,0);
// Saved revisions must never be rolled back by content undo.
stages.coarse.revision=6;ctx.state.savedContent=ctx.semanticContent(stages);ctx.undoEdit();
assert.equal(stages.coarse.revision,6);assert(ctx.state.dirty.fine);assert.equal(stages.fine.revision,7);assert.equal(stages.coarse.source.id,'immutable');
// An invalid pending input is discarded before touching committed undo history.
const past=ctx.state.history.past.length,oldStart=elements.start.value;elements.start.value='-10';ctx.undoEdit();assert.equal(elements.start.value,oldStart);assert.equal(ctx.state.history.past.length,past);
// Merge different actions => unknown, and whole confirmation must reject atomically.
ctx.state.mode='fine';ctx.state.selected.fine=0;frame=0;ctx.render();ctx.merge();assert.equal(stages.fine.segments[0].label,'unknown');
const before=JSON.stringify(stages.fine.segments),count=ctx.state.history.past.length;assert.throws(()=>ctx.confirmAll(),/待标注/);assert.equal(JSON.stringify(stages.fine.segments),before);assert.equal(ctx.state.history.past.length,count);
ctx.undoEdit();assert.equal(stages.fine.segments[0].label,'grasp_truss');frame=5;ctx.split();assert.equal(stages.fine.segments[0].end_frame,5);ctx.undoEdit();assert.equal(stages.fine.segments[0].end_frame,10);
assert(ctx.state.history.past.every(e=>!('revision' in e.snapshot.content.coarse)&&!('source' in e.snapshot.content.fine)));
console.log('subtasks review: multi-step undo redo linked boundaries stage-only bulk confirmation unknown rejection and revision safety passed');
