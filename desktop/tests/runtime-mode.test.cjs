const {test}=require('node:test');
const assert=require('node:assert/strict');
const {runtimeMode}=require('../electron/runtime-mode.cjs');
test('packaged apps reject source launch modes',()=>{
 for(const flag of ['--dev','--local-live'])assert.throws(()=>runtimeMode({packaged:true,argv:['app',flag]}));
 assert.deepEqual(runtimeMode({packaged:true,argv:['app']}),{development:false,localLive:false});
});
test('source daily mode remains distinct from isolated dev mode',()=>{
 assert.deepEqual(runtimeMode({packaged:false,argv:['app','--local-live']}),{development:false,localLive:true});
 assert.deepEqual(runtimeMode({packaged:false,argv:['app','--dev']}),{development:true,localLive:false});
});
