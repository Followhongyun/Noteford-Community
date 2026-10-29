/** Apple-inspired G3 v2. Continuous geometry is canonical, never cubic fitting. */
export const PROFILE=Object.freeze({a:.5,b:.6931765463094081,c:.7131765463094081,extentPerRadius:1.780856513257477});
export const COMPACT=Object.freeze({a:.02,b:.20774288,c:.62909236});
export function controls(profile=PROFILE){const {a,b,c}=profile;return [[0,0],[a,0],[b,0],[c,0],[1,1-c],[1,1-b],[1,1-a],[1,1]];}
export const CONTROL_POINTS=controls();
const ca=.01,cb=.39947887412542604,cc=1.027782788201965,cd=(256-16*ca-56*cb-112*cc)/70;
export const CAP_POINTS=[[0,0],[ca,0],[cb,0],[cc,0],[cd,1],[cc,2],[cb,2],[ca,2],[0,2]];
export function point(t,points=CONTROL_POINTS){const p=points.map(v=>[...v]);for(let n=p.length-1;n>0;n--)for(let i=0;i<n;i++)for(let k=0;k<2;k++)p[i][k]=(1-t)*p[i][k]+t*p[i+1][k];return p[0];}
export function jet(t,points=CONTROL_POINTS){if(!Number.isFinite(t)||t<0||t>1)throw Error('t must be in [0,1]');let p=points.map(v=>[...v]),out=[];for(let o=0;o<=3;o++){out.push(point(t,p));const n=p.length-1;p=p.slice(1).map((v,i)=>v.map((x,k)=>n*(x-p[i][k])));}return out;}
function length(v,size){if(typeof v==='number'){if(!Number.isFinite(v)||v<0)throw Error('Invalid radius');return v;}const m=String(v).trim().match(/^([+]?(?:\d+(?:\.\d*)?|\.\d+))(px|%)?$/);if(!m)throw Error('Unresolved CSS length: use readElementGeometry for calc/em/rem/variables');return Number(m[1])*(m[2]==='%'?size/100:1);}
function expand(v){const a=v.trim().split(/\s+/);if(a.length>4||!a[0])throw Error('Invalid radius shorthand');return a.length===1?[a[0],a[0],a[0],a[0]]:a.length===2?[a[0],a[1],a[0],a[1]]:a.length===3?[a[0],a[1],a[2],a[1]]:a;}
function radii(radius,w,h){
 if(typeof radius==='string'){const a=radius.split('/');if(a.length>2)throw Error('Invalid radius shorthand');const x=expand(a[0]),y=expand(a[1]??a[0]);return x.map((v,i)=>[length(v,w),length(y[i],h)]);}
 return ['tl','tr','br','bl'].map(k=>{let v=typeof radius==='number'?radius:radius?.[k]??0;let pair=Array.isArray(v)?v:typeof v==='object'?[v.x,v.y]:typeof v==='string'?v.trim().split(/\s+/):[v,v];if(pair.length===1)pair=[pair[0],pair[0]];if(pair.length!==2)throw Error('A corner needs two radii');return [length(pair[0],w),length(pair[1],h)];});
}
export function geometry(width,height,radius){
 if(![width,height].every(v=>Number.isFinite(v)&&v>0))throw Error('Positive finite width and height required');
 const resolved=radii(radius,width,height);
 const fit=(size,sum)=>sum>0?size/sum:Infinity;
 const ratio=r=>Math.min(fit(width,r[0][0]+r[1][0]),fit(width,r[3][0]+r[2][0]),fit(height,r[0][1]+r[3][1]),fit(height,r[1][1]+r[2][1]));
 const cssScale=Math.min(1,ratio(resolved)),used=resolved.map(v=>v.some(x=>x===0)?[0,0]:v.map(x=>x*cssScale));
 const multiplier=Math.min(PROFILE.extentPerRadius,ratio(used));
 const blend=Math.max(0,Math.min(1,(multiplier-1)/(PROFILE.extentPerRadius-1)));
 const profile=Object.fromEntries(['a','b','c'].map(k=>[k,COMPACT[k]+blend*(PROFILE[k]-COMPACT[k])]));
 const cp=blend===1?CONTROL_POINTS:controls(profile);
 const close=(a,b)=>Math.abs(a-b)<=1e-10*Math.max(1,a,b);
 let kind='corners';
 if(used.every(v=>close(v[0],width/2)&&close(v[1],height/2)))kind='ellipse';
 else if(used.every(v=>close(v[0],Math.min(width,height)/2)&&close(v[1],Math.min(width,height)/2)))kind='capsule';
 const extents=used.map(v=>v.map(x=>x*multiplier));
 const scalar=v=>v[0]===v[1]?v[0]:v;
 return {width,height,sourceRadius:radius,resolvedRadii:resolved,usedRadii:used,requestedRadii:resolved.map(scalar),effectiveRadii:used.map(scalar),radiusScale:cssScale,cssNormalizationScale:cssScale,cornerExtents:extents,extents:extents.map(v=>v[0]===v[1]?v[0]:v),profileBlend:blend,kind,degree:kind==='ellipse'?null:kind==='capsule'?8:7,controlPoints:kind==='capsule'?CAP_POINTS:cp,closedBoundaryG3:used.every(v=>v[0]>0&&v[1]>0)};
}
function invert(target,axis,cp,hi=.5){let lo=0;for(let i=0;i<38;i++){const m=(lo+hi)/2;if(point(m,cp)[axis]<target)lo=m;else hi=m;}return jet((lo+hi)/2,cp);}
function cornerEvaluator(ex,ey,cp){const cache=new Map(),diag=1-point(.5,cp)[0];return (u,v)=>{
 if(ex===0||ey===0||u>=ex||v>=ey)return Math.min(u,v);
 const U=u/ex,V=v/ey,major=Math.max(U,V),swap=V>U;
 if(major<diag)return (U+V-2*diag)/Math.hypot(1/ex,1/ey);
 const key=major.toPrecision(15);let q=cache.get(key);if(!q){const j=invert(1-major,0,cp);q=[j[0][1],j[1][1]/j[1][0]];cache.set(key,q);}
 return swap?(u-ex*q[0])/Math.hypot(1,ex/ey*q[1]):(v-ey*q[0])/Math.hypot(1,ey/ex*q[1]);
};}
function capField(w,h){const R=h/2,split=point(.25,CAP_POINTS)[0],cache=new Map();return (x,y)=>{
 const t=Math.max(R-x,x-(w-R),0),v=Math.min(y,h-y);if(t===0)return v;
 const axis=t/R<=split?0:1,value=axis===0?t/R:v/R,key=axis+':'+value.toPrecision(15);
 let j=cache.get(key);if(!j){j=invert(Math.max(0,Math.min(1,value)),axis,CAP_POINTS);cache.set(key,j);}
 return axis===0?(v-R*j[0][1])/Math.hypot(1,j[1][1]/j[1][0]):(R*j[0][0]-t)/Math.hypot(1,j[1][0]/j[1][1]);
};}
/** Positive inside; zero set is the exact continuous geometry, not a global SDF. */
export function makeField(g){
 if(g.kind==='ellipse')return (x,y)=>{const rx=g.width/2,ry=g.height/2,X=(x-rx)/rx,Y=(y-ry)/ry,l=Math.hypot(X,Y);return l===0?Math.min(rx,ry):(1-l)/Math.hypot(X/(rx*l),Y/(ry*l));};
 if(g.kind==='capsule'){const horizontal=g.width>g.height,f=capField(Math.max(g.width,g.height),Math.min(g.width,g.height));return (x,y)=>Math.min(x,y,g.width-x,g.height-y,horizontal?f(x,y):f(y,x));}
 const fs=g.cornerExtents.map(([ex,ey])=>cornerEvaluator(ex,ey,g.controlPoints));return (x,y)=>Math.min(x,y,g.width-x,g.height-y,fs[0](x,y),fs[1](g.width-x,y),fs[2](g.width-x,g.height-y),fs[3](x,g.height-y));
}
export function rasterize(width,height,radius,scale=2){
 if(!Number.isFinite(scale)||scale<=0)throw Error('Positive scale required');
 const g=geometry(width,height,radius),w=Math.ceil(width*scale),h=Math.ceil(height*scale);
 if(w*h>32_000_000)throw Error('Mask exceeds 32 million pixels');
 const f=makeField(g),data=new Uint8ClampedArray(w*h*4);
 for(let y=0;y<h;y++)for(let x=0;x<w;x++){const i=4*(y*w+x);data[i]=data[i+1]=data[i+2]=255;data[i+3]=Math.round(255*Math.max(0,Math.min(1,.5+scale*f((x+.5)/scale,(y+.5)/scale))));}
 return {width:w,height:h,data,geometry:g};
}
/** Browser adapter. Apply one returned mask to background/content/border together.
 * Regenerate on layout-size or radius change (ResizeObserver in the host app). */
export function maskCanvas(width,height,radius,scale=globalThis.devicePixelRatio||1){
 const result=rasterize(width,height,radius,scale);const canvas=document.createElement('canvas');canvas.width=result.width;canvas.height=result.height;
 canvas.getContext('2d').putImageData(new ImageData(result.data,result.width,result.height),0,0);return canvas;
}

/** Read CSS used geometry without modifying the element or its declarations.
 * Call again after layout/radius changes. CSS transforms are intentionally excluded. */
export function readElementGeometry(element){
 const doc=element.ownerDocument,win=doc.defaultView,cs=win.getComputedStyle(element);
 const px=k=>parseFloat(cs[k])||0;
 const borderBox=(axis)=>{let n=parseFloat(cs[axis]);if(!Number.isFinite(n))return axis==='width'?element.offsetWidth:element.offsetHeight;if(cs.boxSizing!=='border-box')n+=axis==='width'?px('paddingLeft')+px('paddingRight')+px('borderLeftWidth')+px('borderRightWidth'):px('paddingTop')+px('paddingBottom')+px('borderTopWidth')+px('borderBottomWidth');return n;};
 const w=borderBox('width'),h=borderBox('height');
 // Keep spaces within calc()/min()/max()/clamp() together.
 const tokens=s=>{const out=[];let depth=0,start=0;for(let i=0;i<s.length;i++){if(s[i]==='(')depth++;if(s[i]===')')depth--;if(/\s/.test(s[i])&&!depth){if(i>start)out.push(s.slice(start,i));start=i+1;}}if(start<s.length)out.push(s.slice(start));return out;};
 const resolve=(value,size)=>{try{return length(value,size);}catch{}
  if(/(?:cqw|cqh|cqi|cqb|cqmin|cqmax|lh)\b/.test(value))throw Error('Resolve context-dependent radius in the host framework: '+value);
  if(!win.CSS.supports('width',value))throw Error('Unsupported computed radius: '+value);
  const host=doc.createElement('div'),probe=doc.createElement('div');
  host.style.cssText='all:initial;position:fixed;left:-100000px;top:0;visibility:hidden;pointer-events:none;contain:layout style;';host.style.width=size+'px';
  probe.style.cssText='all:initial;display:block;box-sizing:content-box;min-width:0;max-width:none;padding:0;border:0;';
  for(const k of ['fontSize','fontFamily','fontWeight','fontStyle','fontStretch','letterSpacing'])probe.style[k]=cs[k];
  probe.style.width=value;host.append(probe);doc.documentElement.append(host);
  try{return Math.max(0,probe.getBoundingClientRect().width);}finally{host.remove();}
 };
 const names=['borderTopLeftRadius','borderTopRightRadius','borderBottomRightRadius','borderBottomLeftRadius'],keys=['tl','tr','br','bl'],input={},expressions={};
 names.forEach((name,i)=>{const v=cs[name],a=tokens(v);if(a.length<1||a.length>2)throw Error('Unsupported computed corner: '+v);expressions[keys[i]]=v;input[keys[i]]=[resolve(a[0],w),resolve(a[1]??a[0],h)];});
 const result=geometry(w,h,input);result.computedRadiusExpressions=expressions;return result;
}
