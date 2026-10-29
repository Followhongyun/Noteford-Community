# FFmpeg source and replacement information

Noteford uses libraries from the FFmpeg project under LGPL version 2.1 or later.
Noteford does not own FFmpeg. Original authors, notices and license text are in
the accompanying source archive and runtime/LICENSES.chromium.html.

The v44.4.3 Windows binary provenance below is historical. This security update
only builds and verifies a Mac app locally. A Windows v44.9.1 build must renew
its official ZIP/DLL provenance; the license collector fails closed until then.

## Exact upstream selection

The distributed Windows x64 DLL is unchanged from Electron v44.4.3's official
win32-x64 release, verified against its published SHASUMS256.txt. See
upstream/electron-build/binary-provenance.json for the ZIP and DLL SHA-256.
The current source pin is Electron v44.9.1, selecting Chromium 152.0.7977.167, whose DEPS selects FFmpeg commit
2b68d2babae73714846961fb0ee47e3b3d2e39a9. The complete FFmpeg tree at that commit
and the Electron source (including its patches and build recipes) accompany
this product in sources/. No Noteford changes are made to the library.

Electron's release.gn selects is_component_ffmpeg=true. Its all.gn selects
ffmpeg_branding="Chrome" and proprietary_codecs=true. This preserves AAC/H.264
playback; proprietary_codecs is not the same as FFmpeg's --enable-nonfree.
The checked-in Chrome/win/x64/config.h and config.asm both set CONFIG_GPL,
CONFIG_NONFREE and CONFIG_VERSION3 to zero. The configure invocation is retained
in config.h. Chromium's generated BUILD.gn/file lists and non-upstream changes
are included in the FFmpeg tree. Electron's only FFmpeg patch changes a macOS
loader path; the full original patch and patch order are included.

## Building a compatible modified DLL

Use the version-matched Electron docs/development/build-instructions-windows.md
and build-instructions-gn.md included in the Electron source archive for the
Windows SDK, Visual Studio, depot_tools and GN toolchain setup. In a separate
source workspace, configure gclient with src/electron pointing at the Electron
repository, then sync with src/electron pinned to v44.9.1:

    gclient config --name src/electron --unmanaged https://github.com/electron/electron
    gclient sync --with_branch_heads --with_tags --revision src/electron@v44.9.1

Verify the Electron tag and dependency revisions against the bundled DEPS.
gclient hooks apply Electron's bundled patches. The supplied FFmpeg tree is the
pre-Electron-patch tree. Make your changes in src/third_party/ffmpeg after sync.
Generate an x64 release build from src with these GN arguments in args.gn:

    import("//electron/build/args/release.gn")
    target_cpu = "x64"

Build the ffmpeg target with the toolchain described in the bundled Electron
instructions (for example ninja -C out/Release ffmpeg). Supporting build tools
and dependencies are specified by the version-pinned DEPS and GN files. This
is a source rebuild procedure, not a claim of bit-for-bit local reproducibility.

## Installing a modified DLL

Exit Noteford and its background process. Back up ffmpeg.dll beside Noteford.exe,
then replace it with your Windows x64 DLL built for the same exported API/ABI.
Start Noteford and test playback. Restore the backup if the replacement is not
compatible. Noteford uses this separate DLL and imposes no runtime signature or
hash check on it. Packaging-time checks protect the original distributed build;
they do not restrict users' installed copies. Automatic application updates can
replace the DLL, so retain your modified library separately.

Modification for personal use and reverse engineering for debugging changes to
the LGPL library are expressly permitted by the distribution terms. No app
source disclosure is required merely to use this replaceable shared library.

## Downstream redistribution

Keep the complete license/source materials with the product. If publishing a
download page, state that the product uses FFmpeg under LGPL 2.1 or later and
provide the corresponding source materials on that same download site. Changes
to the DLL, Electron version, build configuration or any dependency require a
fresh source/notice review. Codec patent rights are separate from copyright
licenses and are not granted by this document.
