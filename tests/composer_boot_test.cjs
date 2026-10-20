const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const bridge = fs.readFileSync('backend/erp/domains/pod/composer_batch_bridge.js', 'utf8').split('async function persistErpComposerBatch')[0];
let catalogCalls=0, jobCalls=0, selected=0, artwork=0, releaseCatalog, releaseJobs;
const state={products:[]};
const context={state, Promise, Map, request:async()=>({}), document:{hidden:false},
  window:{sessionStorage:{getItem:()=>JSON.stringify({product:{key:'s2b:1'},items:[{id:'art',thumbnail_url:'/thumb'}]}),removeItem:()=>{}},setInterval:()=>1},
  loadCatalog:()=>{catalogCalls++;return new Promise(resolve=>{releaseCatalog=()=>{state.products=[{id:'s2b:1'}];resolve();};});},
  loadJobs:()=>{jobCalls++;return new Promise(resolve=>{releaseJobs=resolve;});},
  loadStatus:async()=>{},loadComposerCategories:async()=>{},
  productId:p=>p.id,selectProduct:async()=>{selected++;},useArtworkLibraryItem:async()=>{artwork++;},
  setWorkspaceView:()=>{},showNotice:()=>{},updateReadiness:()=>{},renderSelectedProduct:()=>{},updateWorkspace:()=>{}
};
vm.createContext(context);vm.runInContext(bridge,context);
(async()=>{
 const boot=context.initializeErpComposer();
 context.loadCatalog();context.loadJobs();
 assert.equal(catalogCalls,1);assert.equal(jobCalls,1);
 releaseCatalog();await boot; // No wait for the still-pending historical jobs.
 assert.equal(selected,1);assert.equal(artwork,1);assert.equal(state.batchArtworkItems.length,1);
 releaseJobs();await context.loadJobs();
 console.log('Composer boot: shared reads, independent history, and batch selection preserved');
})().catch(e=>{console.error(e);process.exitCode=1;});
