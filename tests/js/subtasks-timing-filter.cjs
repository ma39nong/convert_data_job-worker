const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('src/convest/subtasks/static/app.js','utf8');
const rows=[{id:'conflict',timing_conflict:true},{id:'corrected',timing_resolution:true},{id:'plain'},{id:'saved',coarse_saved:true}].map(e=>({...e,remote_directory:'/normal/full-block-more/A/package',variant:'normal',episode_index:0}));
const c={state:{catalog:{episodes:rows},workflow:'without_return',variant:'normal',filter:'timing',directory:null},$:()=>({value:''})};vm.createContext(c);
for(const name of ['visibleEpisodes','inVariant','taskGroup','compareDirectories','shortDirectory'])vm.runInContext(source.split('\n').find(l=>l.startsWith('function '+name+'(')),c);
assert.deepEqual(Array.from(c.visibleEpisodes(),e=>e.id),['conflict','corrected']);
c.state.filter='coarse';assert.deepEqual(Array.from(c.visibleEpisodes(),e=>e.id),['saved']);
c.state.filter='all';assert.equal(c.visibleEpisodes().length,4);
console.log('timing filter: conflicts and evidence corrections visible, saved filter unchanged');
