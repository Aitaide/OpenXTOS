CC ?= gcc
CFLAGS ?= -std=c11 -Wall -Wextra -Werror -O2 -fPIC
CPPFLAGS ?= -I./include
LDLIBS ?= -lm

# ABI version (keep in sync with include/xtos/signal_version.h)
LIB_MAJOR = 1
LIB_MINOR = 0
OBJS = src/signal.o
LIB_STATIC = libxtos-signal.a
LIB_SHARED = libxtos-signal.so.$(LIB_MAJOR).$(LIB_MINOR)

.PHONY: all clean test

all: $(LIB_STATIC) $(LIB_SHARED)

$(LIB_STATIC): $(OBJS)
	ar rcs $@ $^

$(LIB_SHARED): $(OBJS)
	$(CC) -shared -Wl,-soname,libxtos-signal.so.$(LIB_MAJOR) -o $@ $^ $(LDLIBS)
	ln -sf $(LIB_SHARED) libxtos-signal.so.$(LIB_MAJOR)
	ln -sf libxtos-signal.so.$(LIB_MAJOR) libxtos-signal.so

src/%.o: src/%.c
	$(CC) $(CPPFLAGS) $(CFLAGS) -c $< -o $@

test_signal: tests/test_signal.c $(LIB_STATIC)
	$(CC) $(CPPFLAGS) $(CFLAGS) -o $@ $< $(LIB_STATIC) $(LDLIBS)

test: test_signal
	./test_signal

clean:
	rm -f $(OBJS) $(LIB_STATIC) $(LIB_SHARED) libxtos-signal.so.$(LIB_MAJOR) libxtos-signal.so test_signal
