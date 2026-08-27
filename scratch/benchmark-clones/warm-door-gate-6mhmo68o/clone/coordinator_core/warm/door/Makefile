# Makefile -- builds the POSIX (macOS/Linux) warm-engine door.
#
# UNVERIFIED: this Makefile has never been run. See README-posix.md.
#
# The Windows door (door.c -> door.exe) is NOT built here; it has its own
# builder, `build.py`, and nothing in this file touches it.
#
# Two build-time values are substituted, matching build.py's own
# `__PYTHON_BIN_W__` / `__BUILD_ENGINE_ROOT_W__` convention:
#
#   PYTHON_BIN         interpreter the fallback spawns. Default `python3`,
#                      resolved through PATH by posix_spawnp. Pass an
#                      absolute path to pin it.
#   BUILD_ENGINE_ROOT  LAST-RESORT fallback engine root, used only if the
#                      runtime sidecar/env resolution fails. Left empty by
#                      default ON PURPOSE: an empty/placeholder value fails
#                      `is_valid_engine_root()`, so the door REFUSES loudly
#                      rather than guessing an engine. Never bake a value
#                      you have not checked carries coordinator_core/_engine_stamp.
#
# The socket-derivation path does NOT come from either of these -- it is
# read at runtime from the `door.engine-root.txt` sidecar next to the
# binary. `build_posix.py` writes that sidecar for you and is the
# recommended entry point; this Makefile is the no-Python, bare-hands path.
#
#   make                       # placeholders left in; needs a sidecar
#   make PYTHON_BIN=/usr/bin/python3 BUILD_ENGINE_ROOT=/opt/klabauter
#   make clean

CC ?= cc
PYTHON_BIN ?= python3
BUILD_ENGINE_ROOT ?=

# -O2 for the same reason build.py passes it: this binary's entire purpose
# is to be cheaper than starting an interpreter.
CFLAGS ?= -O2 -Wall -Wextra -std=c11
CPPFLAGS += -DPYTHON_BIN='"$(PYTHON_BIN)"' -DBUILD_ENGINE_ROOT='"$(BUILD_ENGINE_ROOT)"'

TARGET := door
OBJS := door_posix.o door_core.o
SELFTEST := door_core_selftest

.PHONY: all clean selftest check windows-check
all: $(TARGET)

$(TARGET): $(OBJS)
	$(CC) $(CFLAGS) -o $@ $(OBJS)

# RUN THIS FIRST. `door_core_selftest` needs no server, no engine, and no
# Python: it asserts that the shared core -- the SHA-1 that must match
# `hashlib.sha1(...).hexdigest()[:16]`, the envelope reader, and the
# classification deciding whether a delivered request may be re-run --
# behaves identically on your toolchain. If it fails, nothing downstream is
# trustworthy; report the failing check name and stop.
selftest: $(SELFTEST)

$(SELFTEST): door_core_selftest.c door_core.c door_core.h
	$(CC) $(CFLAGS) -o $@ door_core_selftest.c door_core.c

check: $(SELFTEST)
	./$(SELFTEST)

door_posix.o: door_posix.c door_core.h
	$(CC) $(CFLAGS) $(CPPFLAGS) -c -o $@ door_posix.c

door_core.o: door_core.c door_core.h
	$(CC) $(CFLAGS) -c -o $@ door_core.c

# RUN THIS BEFORE COMMITTING ANY CHANGE TO door_core.{c,h}. The shared core is
# linked into the WINDOWS door too, and no Mac can run `door.exe` -- but the
# headers door.c needs (windows.h, shellapi.h, sddl.h) come from mingw-w64, so
# the compile and the LINK are both checkable here. The link is the half that
# matters: it is what catches a door_core.h declaration that no longer matches
# its definition, which is what an extraction into the shared core can break.
#
# Verified toolchain, not just a syntax pass: mingw-w64 is NOT MSVC, and
# build.py ships with cl.exe. A green run here means "well-formed and links",
# never "the Windows build succeeds".
WIN_CC ?= x86_64-w64-mingw32-gcc

windows-check:
	@command -v $(WIN_CC) >/dev/null 2>&1 || { \
	  echo "windows-check: $(WIN_CC) not found -- brew install mingw-w64"; exit 1; }
	$(WIN_CC) $(CFLAGS) -DPYTHON_BIN_W='L"python3"' -DBUILD_ENGINE_ROOT_W='L""' \
	  -c -o door_win_check.o door.c
	$(WIN_CC) $(CFLAGS) -c -o door_core_win_check.o door_core.c
	$(WIN_CC) -O2 -o door_win_check.exe door_win_check.o door_core_win_check.o \
	  -ladvapi32 -lshell32
	@echo "windows-check: door.c + door_core.c compile and link for Windows"
	@rm -f door_win_check.o door_core_win_check.o door_win_check.exe

clean:
	rm -f $(TARGET) $(OBJS) $(SELFTEST) \
	      door_win_check.o door_core_win_check.o door_win_check.exe
