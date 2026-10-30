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
SNAPSHOT_TEST_BINARY = tests/test_snapshot
UAPI_TEST_BINARY = tests/test_snapshot_uapi
CONFIG_TEST_BINARY = tests/test_config
FRAME_TEST_BINARY = tests/test_frame
TEST_BINARIES = $(TEST_BINARY) $(SNAPSHOT_TEST_BINARY) $(UAPI_TEST_BINARY) $(CONFIG_TEST_BINARY) $(FRAME_TEST_BINARY)
HEADERS = include/xtos/bci.h include/xtos/bci_uapi.h include/xtos/bci_snapshot_uapi.h include/xtos/bci_config_uapi.h include/xtos/bci_frame_uapi.h include/xtos/bci_version.h
SNAPSHOT_WRAPS = -Wl,--wrap=open -Wl,--wrap=close -Wl,--wrap=ioctl -Wl,--wrap=mmap -Wl,--wrap=munmap
FRAME_WRAPS = $(SNAPSHOT_WRAPS) -Wl,--wrap=malloc -Wl,--wrap=calloc

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

$(OBJS): $(HEADERS)

%.o: %.c
	$(CC) $(CFLAGS) -c $< -o $@

test: $(TEST_BINARIES)
	./$(TEST_BINARY)
	./$(SNAPSHOT_TEST_BINARY)
	./$(UAPI_TEST_BINARY)
	./$(CONFIG_TEST_BINARY)
	./$(FRAME_TEST_BINARY)

$(TEST_BINARY): tests/test_samples.c $(SRCS) $(HEADERS)
	$(CC) $(CFLAGS) -o $@ tests/test_samples.c $(SRCS) -lm

$(SNAPSHOT_TEST_BINARY): tests/test_snapshot.c $(SRCS) $(HEADERS)
	$(CC) $(CFLAGS) -o $@ tests/test_snapshot.c $(SRCS) $(SNAPSHOT_WRAPS)

$(UAPI_TEST_BINARY): tests/test_snapshot_uapi.c $(HEADERS)
	$(CC) $(CFLAGS) -o $@ tests/test_snapshot_uapi.c

$(CONFIG_TEST_BINARY): tests/test_config.c $(SRCS) $(HEADERS)
	$(CC) $(CFLAGS) -o $@ tests/test_config.c $(SRCS) -Wl,--wrap=open -Wl,--wrap=close -Wl,--wrap=ioctl -Wl,--wrap=read

$(FRAME_TEST_BINARY): tests/test_frame.c $(SRCS) $(HEADERS)
	$(CC) $(CFLAGS) -o $@ tests/test_frame.c $(SRCS) $(FRAME_WRAPS)

clean:
	rm -f $(OBJS) $(LIB_STATIC) $(LIB_SHARED) $(LIB_SONAME) $(LIB_NAME).so
	rm -f $(TEST_BINARIES)

install: all
	install -d $(DESTDIR)/usr/lib
	install -d $(DESTDIR)/usr/include/xtos
	install -m 0755 $(LIB_SHARED) $(DESTDIR)/usr/lib/
	ln -sf $(LIB_SHARED) $(DESTDIR)/usr/lib/$(LIB_SONAME)
	ln -sf $(LIB_SONAME) $(DESTDIR)/usr/lib/$(LIB_NAME).so
	install -m 0644 $(LIB_STATIC) $(DESTDIR)/usr/lib/
	install -m 0644 $(HEADERS) $(DESTDIR)/usr/include/xtos/

uninstall:
	rm -f $(DESTDIR)/usr/lib/$(LIB_SHARED)
	rm -f $(DESTDIR)/usr/lib/$(LIB_SONAME)
	rm -f $(DESTDIR)/usr/lib/$(LIB_NAME).so
	rm -f $(DESTDIR)/usr/lib/$(LIB_STATIC)
	rm -f $(DESTDIR)/usr/include/xtos/bci.h
	rm -f $(DESTDIR)/usr/include/xtos/bci_uapi.h
	rm -f $(DESTDIR)/usr/include/xtos/bci_snapshot_uapi.h
	rm -f $(DESTDIR)/usr/include/xtos/bci_config_uapi.h
	rm -f $(DESTDIR)/usr/include/xtos/bci_frame_uapi.h
	rm -f $(DESTDIR)/usr/include/xtos/bci_version.h
	rmdir --ignore-fail-on-non-empty $(DESTDIR)/usr/include/xtos

.PHONY: all test clean install uninstall
