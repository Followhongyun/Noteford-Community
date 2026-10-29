// Creates a local source launcher; never builds or replaces a distributed app.
'use strict';
const fs=require('node:fs'), path=require('node:path'), os=require('node:os');
if(process.platform!=='darwin')throw Error('This entry is for macOS');
const desktop=path.resolve(__dirname,'..');
const name='Noteford';
const app=path.join(os.homedir(),'Applications',name+' 开发版.app');
const marker=path.join(app,'Contents','source-launcher.json');
if(fs.existsSync(app)&&(!fs.existsSync(marker)||JSON.parse(fs.readFileSync(marker,'utf8')).desktop!==desktop))throw Error('Existing entry belongs to another application: '+app);
const contents=path.join(app,'Contents');
fs.mkdirSync(path.join(contents,'MacOS'),{recursive:true});
fs.mkdirSync(path.join(contents,'Resources'),{recursive:true});
const quote=value=>"'"+value.replaceAll("'","'\"'\"'")+"'";
fs.writeFileSync(path.join(contents,'MacOS','launch'),'#!/bin/bash\nexec '+quote(process.execPath)+' '+quote(path.join(__dirname,'daily-launch.cjs'))+'\n',{mode:0o755});
fs.writeFileSync(path.join(contents,'Info.plist'),`<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>CFBundleExecutable</key><string>launch</string><key>CFBundleIdentifier</key><string>local.source.${name.toLowerCase()}.launcher</string><key>CFBundleName</key><string>${name} 开发版</string><key>CFBundlePackageType</key><string>APPL</string><key>CFBundleIconFile</key><string>app.icns</string><key>LSUIElement</key><true/></dict></plist>`);
fs.copyFileSync(path.join(desktop,'assets/noteford.icns'),path.join(contents,'Resources/app.icns'));
fs.writeFileSync(marker,JSON.stringify({desktop}));
console.log(app);
