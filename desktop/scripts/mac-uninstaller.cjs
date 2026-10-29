'use strict';
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
function build(app) {
  const bundle = path.join(app, 'Contents/Helpers/卸载 Noteford.app');
  fs.mkdirSync(path.join(bundle,'Contents/MacOS'),{recursive:true});
  fs.mkdirSync(path.join(bundle,'Contents/Resources'),{recursive:true});
  fs.cpSync(path.join(app,'Contents/Resources/service'),path.join(bundle,'Contents/Resources/service'),{recursive:true, verbatimSymlinks:true});
  fs.copyFileSync(path.join(app,'Contents/Resources/icon.icns'),path.join(bundle,'Contents/Resources/icon.icns'));
  fs.writeFileSync(path.join(bundle,'Contents/Info.plist'),`<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd"><plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>com.followhongyun.noteford.uninstaller</string>
<key>CFBundleName</key><string>卸载 Noteford</string><key>CFBundleDisplayName</key><string>卸载 Noteford</string>
<key>CFBundleExecutable</key><string>NotefordUninstaller</string><key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleVersion</key><string>1</string><key>CFBundleIconFile</key><string>icon.icns</string>
<key>NSHighResolutionCapable</key><true/></dict></plist>`);
  execFileSync('/usr/bin/xcrun',['swiftc',path.join(__dirname,'../native/uninstall.swift'),'-o',path.join(bundle,'Contents/MacOS/NotefordUninstaller'),'-framework','AppKit','-target','arm64-apple-macos12.0'],{stdio:'inherit'});
  return bundle;
}
module.exports = {build};
