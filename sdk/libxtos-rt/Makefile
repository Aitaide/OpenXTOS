CC ?= gcc
AR ?= ar
CFLAGS ?= -Wall -Wextra -Werror -O2 -fPIC
CPPFLAGS ?= -I./include
LDFLAGS ?=

LIB_NAME = libxtos-rt
OBJS = src/rt.o src/spsc.o

.PHONY: all clean test

all: $(LIB_NAME).a $(LIB_NAME).so.1.0

$(LIB_NAME).a: $(OBJS)
	$(AR) rcs $@ $^

$(LIB_NAME).so.1.0: $(OBJS)
	$(CC) -shared -Wl,-soname,$(LIB_NAME).so.1 $(LDFLAGS) -o $@ $^
	ln -sf $(LIB_NAME).so.1.0 $(LIB_NAME).so.1
	ln -sf $(LIB_NAME).so.1 $(LIB_NAME).so

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
	rm -f $(OBJS) $(LIB_NAME).a $(LIB_NAME).so.1.0 $(LIB_NAME).so.1 $(LIB_NAME).so test_rt test_spsc test_spsc_tsan
