CC ?= gcc
AR ?= ar
CFLAGS ?= -Wall -Wextra -Werror -O2 -fPIC
CPPFLAGS ?= -I./include
LDFLAGS ?=

# ABI version (keep in sync with include/xtos/rt_version.h)
LIB_NAME = libxtos-rt
LIB_MAJOR = 1
LIB_MINOR = 0
LIB_SHARED = $(LIB_NAME).so.$(LIB_MAJOR).$(LIB_MINOR)
LIB_SONAME = $(LIB_NAME).so.$(LIB_MAJOR)

OBJS = src/rt.o src/spsc.o

.PHONY: all clean test

all: $(LIB_NAME).a $(LIB_SHARED)

$(LIB_NAME).a: $(OBJS)
	$(AR) rcs $@ $^

$(LIB_SHARED): $(OBJS)
	$(CC) -shared -Wl,-soname,$(LIB_SONAME) $(LDFLAGS) -o $@ $^
	ln -sf $(LIB_SHARED) $(LIB_SONAME)
	ln -sf $(LIB_SONAME) $(LIB_NAME).so

src/%.o: src/%.c
	$(CC) $(CPPFLAGS) $(CFLAGS) -c $< -o $@

test_rt: tests/test_rt.c $(LIB_NAME).a
	$(CC) $(CPPFLAGS) $(CFLAGS) -o $@ $< $(LIB_NAME).a

test_spsc: tests/test_spsc.c $(LIB_NAME).a
	$(CC) $(CPPFLAGS) $(CFLAGS) -pthread -o $@ $< $(LIB_NAME).a

test: test_rt test_spsc
	./test_rt
	./test_spsc

clean:
	rm -f $(OBJS) $(LIB_NAME).a $(LIB_SHARED) $(LIB_SONAME) $(LIB_NAME).so test_rt test_spsc test_spsc_tsan
