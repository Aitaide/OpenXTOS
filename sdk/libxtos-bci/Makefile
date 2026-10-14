CC ?= gcc
AR ?= ar
CFLAGS = -Wall -Wextra -O2 -fPIC -I./include
LDFLAGS = -shared

# Library name and ABI version (keep in sync with include/xtos/bci_version.h)
LIB_NAME = libxtos-bci
LIB_MAJOR = 1
LIB_MINOR = 0
LIB_STATIC = $(LIB_NAME).a
LIB_SHARED = $(LIB_NAME).so.$(LIB_MAJOR).$(LIB_MINOR)
LIB_SONAME = $(LIB_NAME).so.$(LIB_MAJOR)
TEST_BINARY = tests/test_samples

# Source files
SRCS = src/session.c
OBJS = $(SRCS:.c=.o)

# Build targets
all: $(LIB_STATIC) $(LIB_SHARED)

$(LIB_STATIC): $(OBJS)
	$(AR) rcs $@ $^

$(LIB_SHARED): $(OBJS)
	$(CC) $(LDFLAGS) -Wl,-soname,$(LIB_SONAME) -o $@ $^
	ln -sf $(LIB_SHARED) $(LIB_SONAME)
	ln -sf $(LIB_SONAME) $(LIB_NAME).so

%.o: %.c
	$(CC) $(CFLAGS) -c $< -o $@

test: $(TEST_BINARY)
	./$(TEST_BINARY)

$(TEST_BINARY): tests/test_samples.c $(SRCS) include/xtos/bci.h include/xtos/bci_uapi.h
	$(CC) $(CFLAGS) -o $@ tests/test_samples.c $(SRCS) -lm

clean:
	rm -f $(OBJS) $(LIB_STATIC) $(LIB_SHARED) $(LIB_SONAME) $(LIB_NAME).so

install: all
	install -d $(DESTDIR)/usr/lib
	install -d $(DESTDIR)/usr/include/xtos
	install -m 0755 $(LIB_SHARED) $(DESTDIR)/usr/lib/
	ln -sf $(LIB_SHARED) $(DESTDIR)/usr/lib/$(LIB_SONAME)
	ln -sf $(LIB_SONAME) $(DESTDIR)/usr/lib/$(LIB_NAME).so
	install -m 0644 $(LIB_STATIC) $(DESTDIR)/usr/lib/
	install -m 0644 include/xtos/bci.h $(DESTDIR)/usr/include/xtos/

uninstall:
	rm -f $(DESTDIR)/usr/lib/$(LIB_SHARED)
	rm -f $(DESTDIR)/usr/lib/$(LIB_SONAME)
	rm -f $(DESTDIR)/usr/lib/$(LIB_NAME).so
	rm -f $(DESTDIR)/usr/lib/$(LIB_STATIC)
	rm -f $(DESTDIR)/usr/include/xtos/bci.h
	rmdir --ignore-fail-on-non-empty $(DESTDIR)/usr/include/xtos

.PHONY: all clean install uninstall
