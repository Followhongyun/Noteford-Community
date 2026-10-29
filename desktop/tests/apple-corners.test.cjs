'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {validate, createNativeCorners} = require('../electron/mac-corners.cjs');

test('optimized G3 masks exactly match the canonical rasterizer, including pills, ellipses and sharp corners', async () => {
 const {geometry, rasterize} = await import('../src/apple-corners/g3-corners.mjs');
 const {contourPixels} = await import('../src/apple-corners/paint.mjs');
 for (const [w,h,r,s] of [[220,130,12,2],[36,22,22,2],[44,44,'50%',2],[40,20,'50%',1.5],[133.25,77.5,{tl:18,tr:0,br:5,bl:12},2],[99,65,'12px 9px / 8px 15px',1]]) {
  const actual=contourPixels(geometry(w,h,r),s,[1,1,1,1]);
  assert.deepEqual(actual.outer,rasterize(w,h,r,s).data,JSON.stringify([w,h,r,s]));
  for(let i=3;i<actual.outer.length;i+=4) assert.ok(actual.strokes.reduce((sum,side)=>sum+(side?.[i]||0),0)<=actual.outer[i]);
 }
});

test('native path preserves radius and sharp corners; asymmetric or elliptical styles use G3', async () => {
 const {geometry}=await import('../src/apple-corners/g3-corners.mjs');
 const {nativeRequest}=await import('../src/apple-corners/paint.mjs');
 const widths=[1,1,1,1], colors=['black','black','black','black'];
 assert.deepEqual(nativeRequest(geometry(200,100,12),2,widths,colors),{width:200,height:100,radius:12,scale:2,stroke:1,corners:15});
 assert.equal(nativeRequest(geometry(200,100,{tl:12,tr:0,br:12,bl:0}),2,widths,colors),null);
 assert.equal(nativeRequest(geometry(200,100,'12px / 8px'),2,widths,colors),null);
 assert.equal(nativeRequest(geometry(200,100,'12px 8px'),2,widths,colors),null);
 assert.equal(nativeRequest(geometry(200,100,12),2,[0,1,0,0],colors),null);
});

test('native IPC accepts only bounded numeric drawing geometry', () => {
 const good={width:200,height:100,radius:12,scale:2,stroke:1,corners:15};
 assert.deepEqual(validate({...good,secret:'discard'}),good);
 for(const patch of [{width:0},{radius:-1},{scale:0},{width:8192,height:8192},{corners:16},{stroke:Infinity},{width:'200'}]) assert.throws(()=>validate({...good,...patch}));
});

test('Mac native continuous API renders in a worker and shuts down cleanly', {skip:process.platform!=='darwin'}, async () => {
 const renderer=createNativeCorners();
 const request={width:200,height:100,radius:12,scale:2,stroke:1,corners:15};
 try {
  const first=renderer.render(request);
  assert.equal(first,renderer.render(request));
  const result=await first;
  assert.equal(result.renderer,'macos-continuous');
  for(const uri of [result.outer,result.stroke]) {
   const png=Buffer.from(uri.split(',')[1],'base64');
   assert.equal(png.subarray(1,4).toString(),'PNG');
   assert.equal(png.readUInt32BE(16),400);
   assert.equal(png.readUInt32BE(20),200);
  }
  const draw=require('../electron/mac-corner-renderer.cjs').createRenderer();
  assert.notEqual(draw({...request,stroke:0},'continuous'),draw({...request,stroke:0},'circular'));
 } finally {renderer.close()}
 assert.equal(await renderer.render(request),null);
});
