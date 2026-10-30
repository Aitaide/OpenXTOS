# SPDX-License-Identifier: Apache-2.0
# Fuzz drivers for XTOS user-space parsers (stage 5).
#
# builds ASan+UBSan harnesses, generates a deterministic corpus, runs every
# corpus entry through each harness, and fails on any nonzero exit (crash,
# sanitizer abort, invariant violation).

CC ?= gcc
CFLAGS ?= -std=c11 -Wall -Wextra -O1 -g -fsanitize=address,undefined -fno-sanitize-recover=all -fno-omit-frame-pointer
CPPFLAGS = -I../../sdk/libxtos-rt/include -I../../sdk/libxtos-signal/include
FUZZ_RT_SRC = ../../sdk/libxtos-rt/src/spsc.c
FUZZ_SIGNAL_SRC = ../../sdk/libxtos-signal/src/signal.c

.PHONY: all clean fuzz check

all: fuzz_csv_parse fuzz_openbci_packet

fuzz_csv_parse: fuzz_csv_parse.c $(FUZZ_RT_SRC) $(FUZZ_SIGNAL_SRC) \
                ../../sdk/libxtos-rt/include/xtos/spsc.h \
                ../../sdk/libxtos-signal/include/xtos/signal.h
	$(CC) $(CPPFLAGS) $(CFLAGS) -o $@ fuzz_csv_parse.c $(FUZZ_RT_SRC) $(FUZZ_SIGNAL_SRC) -lm

fuzz_openbci_packet: fuzz_openbci_packet.c ../../kernel/linux/drivers/xtos/bci/xtos_cyton_packet.h
	$(CC) $(CFLAGS) -o $@ fuzz_openbci_packet.c -I../../kernel/linux/drivers/xtos/bci

# Generate deterministic corpus and run both harnesses over it.
# Any harness abort (crash / sanitizer trap) fails make.
fuzz: all
	rm -rf corpus
	python3 gen_corpus.py --out corpus --rounds $${FUZZ_ROUNDS:-200}
	UBSAN_OPTIONS=halt_on_error=1 ./fuzz_openbci_packet corpus/packet/* > /dev/null
	UBSAN_OPTIONS=halt_on_error=1 ./fuzz_csv_parse corpus/csv/* > /dev/null
	@echo "fuzz_run=pass"

# Light check used by scripts/test.sh: seeds only, few rounds.
check: all
	rm -rf corpus-check
	python3 gen_corpus.py --out corpus-check --rounds $${FUZZ_ROUNDS:-40} --seed 7
	UBSAN_OPTIONS=halt_on_error=1 ./fuzz_openbci_packet corpus-check/packet/* > /dev/null
	UBSAN_OPTIONS=halt_on_error=1 ./fuzz_csv_parse corpus-check/csv/* > /dev/null
	python3 gen_corpus.py --out corpus-check --check
	rm -rf corpus-check
	@echo "fuzz_check=pass"

clean:
	rm -f fuzz_csv_parse fuzz_openbci_packet
	rm -rf corpus corpus-check
