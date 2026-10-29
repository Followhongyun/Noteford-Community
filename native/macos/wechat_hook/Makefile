# Build on macOS. Linux does not have the macOS SDK or CommonCrypto headers.
ARCHS ?= arm64 x86_64
SDK := $(shell xcrun --show-sdk-path)
CFLAGS := -dynamiclib -O2 -Wall -Wextra -fvisibility=hidden -mmacosx-version-min=11.0

libnoteford_wechat_hook.dylib: hook.c
	clang $(CFLAGS) -isysroot $(SDK) $(addprefix -arch ,$(ARCHS)) -o $@ hook.c

clean:
	rm -f libnoteford_wechat_hook.dylib

.PHONY: clean
