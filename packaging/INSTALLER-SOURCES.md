# Installer component sources and notices

The installer is NSIS 3.0.4.1 as packaged by electron-builder 26.15.3.
Original notices are under upstream/installer/. NSIS's zlib-licensed core and
standard plug-ins are used without changes. UAC is unmodified and zlib licensed.
The installed application and its installer remain separate works from these
components; their third-party ownership and licenses are retained.

StdUtils DLL version 1.1.4.0 corresponds to source tag 1.14. The complete source
archive, LGPL 2.1 text and author's proprietary-installer clarification are
included. Build instructions and Visual Studio project files are in that archive.

Nsis7z DLL version 19.00.0.0 byte-matches the x86-unicode DLL in the accompanying
official Nsis7z_19.00.7z source/binary distribution (SHA-256
b393f05e8ff919ef071181050e1873c9a776e1a0ae8329aefff7007d0cadf592).
The companion 7z1900-src.7z provides the full underlying 7-Zip source. Overlay
Contrib/nsis7z onto that source tree and use CPP/7zip/Bundles/Nsis7z/Nsis7z.sln
to rebuild. Keep all copyright and applicable LGPL/BSD/public-domain notices.
The source archive also contains unRAR code and its separate restriction; the
Nsis7z library used here extracts only the installation's 7z payload.

To use a modified compatible installer plug-in, rebuild the installer with your
DLL in electron-builder's NSIS plug-in directory. Modification and reverse
engineering for debugging LGPL component modifications are permitted. The
components are loaded as separate DLLs through the NSIS interface; Noteford does
not impose a signature or hash restriction on user-rebuilt installers. Retain
source changes under their applicable licenses when redistributing them.

WinShell's upstream archive contains binaries but no clear redistribution
terms. It is omitted: ten optional shell-history/unpin and shortcut AppUserModelID operations
are removed from the MIT-licensed electron-builder installer/uninstaller templates by
desktop/scripts/prepare-installer-template.cjs. Standard shortcut deletion,
installation, uninstallation and updating remain in the original template.
The source patch is identified explicitly as a Noteford modification.

The separate elevate.exe helper is not distributed. This build installs for the
current Windows user only; it does not offer an all-users installation. Updates
of this current-user installation continue to use electron-updater normally.
Microsoft runtime restrictions in DISTRIBUTION-TERMS apply only to Microsoft's
code, never to these libraries' independently granted open-source rights.

Ordinary shortcut targets remain intact. Noteford sets its own process AppUserModelID when starting; the installer no longer annotates shortcut files with that property.
