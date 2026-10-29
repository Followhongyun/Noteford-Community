'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawn, spawnSync } = require('node:child_process');
const { waitForRenderer } = require('./renderer-ready.cjs');
const desktop = path.resolve(__dirname, '..');
const project = path.dirname(desktop);
const {sourceLayout,prepareSourceRuntime,prepareSourcePython}=require('./source-runtime.cjs');
const layout=sourceLayout(desktop);
const dataRoot = process.env.NOTEFORD_DATA_ROOT || (process.platform==='darwin'?layout.data:undefined);
let python = process.env.NOTEFORD_PYTHON || (process.platform==='darwin'?layout.python:undefined);
let electron;
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
function isReloadable(file) { return /\.(py|cjs)$/.test(file) && !file.split(/[\\/]/).includes('__pycache__'); }
function digest(file) { try { return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex'); } catch (e) { if (e.code === 'ENOENT') return null; throw e; } }
async function run() {
  if (!dataRoot || !path.isAbsolute(dataRoot) || !fs.existsSync(process.platform==='darwin'?dataRoot:path.join(dataRoot, 'config.local.json'))) throw Error('An existing absolute data root is required');
  electron=prepareSourceRuntime(desktop,layout.cache);
  if (!python || !fs.existsSync(python) || !fs.existsSync(electron)) throw Error('Local Python or Electron runtime is missing');
  python=prepareSourcePython(desktop,layout.cache,python);
  const stateRoot = process.platform==='darwin'?layout.cache:path.join(dataRoot, 'data/runtime/local-live');
  fs.mkdirSync(stateRoot, {recursive:true});
  const lock = path.join(stateRoot, 'runner.lock');
  const env = {...process.env,NOTEFORD_DATA_ROOT:dataRoot,NOTEFORD_PYTHON:python}; delete env.ELECTRON_RUN_AS_NODE;
  // An existing supervisor owns the watchers; Electron's instance lock activates its window.
  if (fs.existsSync(lock)) {
    const owner = JSON.parse(fs.readFileSync(lock, 'utf8'));
    let alive = true;
    try { process.kill(owner.pid, 0); } catch (e) { if (e.code === 'ESRCH') alive = false; else throw e; }
    if (alive) {
      await new Promise((resolve,reject) => { const child=spawn(electron,[desktop,'--local-live',...(process.env.NOTEFORD_BACKGROUND==='1'?['--background']:[])],{env,windowsHide:false,stdio:'ignore'});child.once('error',reject);child.once('exit',code=>code===0?resolve():reject(Error('Could not activate application'))); });
      return;
    }
    fs.unlinkSync(lock);
  }
  fs.writeFileSync(lock, JSON.stringify({pid:process.pid}), {flag:'wx'});
  const output = fs.openSync(path.join(stateRoot, 'runner.log'), 'a');
  const log = text => fs.writeSync(output, `${new Date().toISOString()} ${text}\n`);
  let vite, app, timer, stopping=false, restarting=false;
  let restarts=0, crashRetries=0, readiness=null;
  const watchers=[], hashes=new Map(), changed=new Set();
  const status = phase => fs.writeFileSync(path.join(stateRoot,'status.json'), JSON.stringify({phase,pid:process.pid,appPid:app?.pid,restarts,project,dataRoot,readiness,updatedAt:new Date().toISOString()}));
  const stop = async child => {
    if (!child || child.exitCode !== null) return;
    const exited = new Promise(resolve=>child.once('exit',resolve));
    if(child===app&&child.connected)child.send({type:'noteford:reload'});else child.kill();
    await Promise.race([exited,delay(10000)]);
    if(child===app&&child.exitCode===null)throw Error('源码应用尚未正常退出，请在应用菜单退出后重试');
  };
  const launch = () => {
    readiness=null;
    // Electron is a GUI process. SW_HIDE would suppress its first window.show();
    // background visibility is controlled by --background inside the app.
    app=spawn(electron,[desktop,'--local-live',...(process.env.NOTEFORD_BACKGROUND==='1'?['--background']:[]),...(process.env.NOTEFORD_TEST_DEBUG_PORT ? ['--remote-debugging-address=127.0.0.1','--remote-debugging-port='+process.env.NOTEFORD_TEST_DEBUG_PORT] : [])],{env,windowsHide:false,stdio:['ignore',output,output,'ipc']});
    app.once('error',e=>{log(e.message);void shutdown(1)});
    app.on('message',message=>{
      if(message?.type==='noteford:request-restart' && !stopping && !restarting){
        void reload({force:true}).catch(error=>{log(error.message);status('restart-error')});
        return;
      }
      if(message?.type==='noteford:ready' && message.runtimeRoot===dataRoot && !stopping){
        readiness={runtimeRoot:message.runtimeRoot,servicePort:message.servicePort,servicePid:message.servicePid,rendererLoaded:true};
        log('Application renderer loaded');status('ready');
      }
    });
    app.once('exit',code=>{
      if(restarting||stopping)return;
      if(code!==0 && crashRetries++<3){log('Application exited unexpectedly; retrying in 5 seconds');status('recovering');timer=setTimeout(()=>{if(!stopping){restarts++;launch();}},5000);}
      else void shutdown(code||0);
    });
    log('Application launched');status('running');
  };
  const shutdown = async code => {
    if(stopping)return;stopping=true;clearTimeout(timer);watchers.forEach(w=>w.close());
    try { await stop(app); } catch(error) { log(error.message); status('quit-pending'); stopping=false; return; }
    await stop(vite);status('stopped');fs.unlinkSync(lock);fs.closeSync(output);process.exit(code);
  };
  async function reload({force=false}={}) {
    if(stopping || restarting || (!force && !changed.size))return;
    const files=[...changed];changed.clear();
    for(const file of files){
      if(!fs.existsSync(file))continue;
      const args=file.endsWith('.py') ? ['-c','import ast,sys; from pathlib import Path; ast.parse(Path(sys.argv[1]).read_text(encoding="utf-8-sig"))',file] : ['--check',file];
      const result=spawnSync(file.endsWith('.py')?python:process.execPath,args,{windowsHide:true,encoding:'utf8'});
      if(result.status!==0){log('Reload skipped: syntax check failed for '+path.relative(project,file));status('syntax-error');return;}
    }
    restarting=true;status('reloading');
    const old=app;
    const exited=new Promise(resolve=>old.once('exit',()=>resolve(true)));
    if(old.connected)old.send({type:'noteford:reload'});
    if(!await Promise.race([exited,delay(10000).then(()=>false)])){
      log('Reload stopped: application did not quit safely');restarting=false;status('restart-timeout');return;
    }
    restarts++;launch();restarting=false;
    if(changed.size)timer=setTimeout(()=>void reload(),800);
  }
  try {
    // Watch only application code, never databases, secrets, generated files or dependencies.
    for(const root of [path.join(project,'src'),path.join(desktop,'electron')]){
      const collect=dir=>{for(const entry of fs.readdirSync(dir,{withFileTypes:true})){const file=path.join(dir,entry.name);if(entry.isDirectory() && entry.name!=='__pycache__')collect(file);else if(entry.isFile()&&isReloadable(file))hashes.set(file,digest(file));}};collect(root);
      watchers.push(fs.watch(root,{recursive:true},(_event,name)=>{
        if(!name || !isReloadable(name))return;
        const file=path.join(root,name),hash=digest(file);
        if(hashes.get(file)===hash)return;hashes.set(file,hash);changed.add(file);
        clearTimeout(timer);timer=setTimeout(()=>void reload().catch(e=>{log(e.message);void shutdown(1)}),800);
      }));
    }
    status('starting');
    vite=spawn(process.execPath,[path.join(desktop,'node_modules/vite/bin/vite.js'),'--host','127.0.0.1','--port','5187','--strictPort'],{cwd:desktop,env,windowsHide:true,stdio:['ignore','pipe','pipe']});
    vite.once('exit',(code,signal)=>{if(app&&!stopping){log(`Renderer stopped (code ${code}, signal ${signal})`);void shutdown(1);}});
    await waitForRenderer(vite, {write:chunk=>fs.writeSync(output,chunk)});
    launch();
    process.on('SIGINT',()=>void shutdown(0));process.on('SIGTERM',()=>void shutdown(0));
  } catch(e){log(e.message);await shutdown(1);}
}
module.exports={isReloadable,digest};
if(require.main===module)run().catch(e=>{console.error(e.message);process.exitCode=1});
