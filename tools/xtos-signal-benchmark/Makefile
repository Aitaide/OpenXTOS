CC ?= gcc
CFLAGS ?= -std=c11 -Wall -Wextra -Werror -O2
CPPFLAGS ?= -I../../sdk/libxtos-signal/include
LDLIBS ?= -lm
SIGNAL_SRC = ../../sdk/libxtos-signal/src/signal.c

.PHONY: all clean test

all: xtos-signal-benchmark

xtos-signal-benchmark: benchmark.c $(SIGNAL_SRC) ../../sdk/libxtos-signal/include/xtos/signal.h
	$(CC) $(CPPFLAGS) $(CFLAGS) -o $@ benchmark.c $(SIGNAL_SRC) $(LDLIBS)

test: xtos-signal-benchmark
	python3 -m unittest -v test_benchmark.py

clean:
	rm -f xtos-signal-benchmark
