CC ?= gcc
CFLAGS ?= -std=c11 -Wall -Wextra -Werror -O2 -fPIC
CPPFLAGS ?= -I./include
LDLIBS ?= -lm

# ABI version (keep in sync with include/xtos/signal_version.h)
LIB_MAJOR = 1
LIB_MINOR = 1
OBJS = src/signal.o src/eeg_window.o
LIB_STATIC = libxtos-signal.a
LIB_SHARED = libxtos-signal.so.$(LIB_MAJOR).$(LIB_MINOR)

.PHONY: all clean test install uninstall

all: $(LIB_STATIC) $(LIB_SHARED)

$(LIB_STATIC): $(OBJS)
	$(AR) rcs $@ $^

$(LIB_SHARED): $(OBJS)
	$(CC) -shared -Wl,-soname,libxtos-signal.so.$(LIB_MAJOR) -o $@ $^ $(LDLIBS)
	ln -sf $(LIB_SHARED) libxtos-signal.so.$(LIB_MAJOR)
	ln -sf libxtos-signal.so.$(LIB_MAJOR) libxtos-signal.so

src/%.o: src/%.c
	$(CC) $(CPPFLAGS) $(CFLAGS) -c $< -o $@

test_signal: tests/test_signal.c $(LIB_STATIC)
	$(CC) $(CPPFLAGS) $(CFLAGS) -o $@ $< $(LIB_STATIC) $(LDLIBS)

test_eeg_window: tests/test_eeg_window.c $(LIB_STATIC)
	$(CC) $(CPPFLAGS) $(CFLAGS) -o $@ $< $(LIB_STATIC) $(LDLIBS)

test: test_signal test_eeg_window
	./test_signal
	@set -e; for mode in golden boundaries quality freshness rolling bounds invalid duplicate oldest_stale; do ./test_eeg_window $$mode; done
	@printf 'PASS eeg_window_cases=9\n'

HEADERS = include/xtos/signal.h include/xtos/signal_version.h include/xtos/eeg_window.h
$(OBJS): $(HEADERS)

install: all
	install -d $(DESTDIR)/usr/lib $(DESTDIR)/usr/include/xtos
	install -m 0755 $(LIB_SHARED) $(DESTDIR)/usr/lib/
	ln -sf $(LIB_SHARED) $(DESTDIR)/usr/lib/libxtos-signal.so.$(LIB_MAJOR)
	ln -sf libxtos-signal.so.$(LIB_MAJOR) $(DESTDIR)/usr/lib/libxtos-signal.so
	install -m 0644 $(LIB_STATIC) $(DESTDIR)/usr/lib/
	install -m 0644 $(HEADERS) $(DESTDIR)/usr/include/xtos/

uninstall:
	rm -f $(DESTDIR)/usr/lib/$(LIB_SHARED) $(DESTDIR)/usr/lib/$(LIB_STATIC)
	rm -f $(DESTDIR)/usr/lib/libxtos-signal.so.$(LIB_MAJOR) $(DESTDIR)/usr/lib/libxtos-signal.so
	rm -f $(addprefix $(DESTDIR)/usr/,$(HEADERS))
	rmdir --ignore-fail-on-non-empty $(DESTDIR)/usr/include/xtos

clean:
	rm -f $(OBJS) $(LIB_STATIC) $(LIB_SHARED) libxtos-signal.so.1.0 libxtos-signal.so.$(LIB_MAJOR) libxtos-signal.so test_signal test_eeg_window
