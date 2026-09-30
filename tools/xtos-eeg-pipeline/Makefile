CC ?= gcc
CFLAGS ?= -std=c11 -Wall -Wextra -Werror -O2
CPPFLAGS ?= -I../../sdk/libxtos-rt/include -I../../sdk/libxtos-signal/include
LDFLAGS ?= -lm
SIGNAL_SRC = ../../sdk/libxtos-signal/src/signal.c

.PHONY: all clean test

all: xtos-eeg-pipeline

xtos-eeg-pipeline: pipeline.c ../../sdk/libxtos-rt/src/spsc.c ../../sdk/libxtos-rt/include/xtos/spsc.h $(SIGNAL_SRC) ../../sdk/libxtos-signal/include/xtos/signal.h
	$(CC) $(CPPFLAGS) $(CFLAGS) -o $@ pipeline.c ../../sdk/libxtos-rt/src/spsc.c $(SIGNAL_SRC) $(LDFLAGS)

test: xtos-eeg-pipeline
	python3 -m unittest -v test_pipeline.py

clean:
	rm -f xtos-eeg-pipeline
