CC ?= gcc
CFLAGS ?= -std=c11 -Wall -Wextra -Werror -O2 -fPIC
CPPFLAGS ?= -I./include
LDLIBS ?= -lm

OBJS = src/signal.o
LIB_STATIC = libxtos-signal.a
LIB_SHARED = libxtos-signal.so.1.0

.PHONY: all clean test

all: $(LIB_STATIC) $(LIB_SHARED)

$(LIB_STATIC): $(OBJS)
	ar rcs $@ $^

$(LIB_SHARED): $(OBJS)
	$(CC) -shared -Wl,-soname,libxtos-signal.so.1 -o $@ $^ $(LDLIBS)
	ln -sf $(LIB_SHARED) libxtos-signal.so.1
	ln -sf libxtos-signal.so.1 libxtos-signal.so

src/%.o: src/%.c
	$(CC) $(CPPFLAGS) $(CFLAGS) -c $< -o $@

test_signal: tests/test_signal.c $(LIB_STATIC)
	$(CC) $(CPPFLAGS) $(CFLAGS) -o $@ $< $(LIB_STATIC) $(LDLIBS)

test: test_signal
	./test_signal

clean:
	rm -f $(OBJS) $(LIB_STATIC) $(LIB_SHARED) libxtos-signal.so.1 libxtos-signal.so test_signal
