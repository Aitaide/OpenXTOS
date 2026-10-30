CC ?= gcc
CFLAGS ?= -std=c11 -Wall -Wextra -Werror -O2
CPPFLAGS ?= -I../../sdk/libxtos-rt/include -I../../sdk/libxtos-signal/include -I../../sdk/libxtos-bci/include
LDLIBS ?= -lm -pthread
RT_SRC = ../../sdk/libxtos-rt/src/spsc.c
SIGNAL_SRC = ../../sdk/libxtos-signal/src/signal.c
BCI_SRC = ../../sdk/libxtos-bci/src/session.c
BCI_HEADERS = $(wildcard ../../sdk/libxtos-bci/include/xtos/*.h)
CALIBRATION_SRC = bci_calibration_cli.c bci_calibration.c
CALIBRATION_HEADERS = bci_calibration_cli.h bci_calibration.h bci_sha256.h
DIAGNOSTIC_SRC = bci_diagnostic_cli.c cyton_diagnostic_serial.c bci_diagnostic.c
DIAGNOSTIC_HEADERS = bci_diagnostic_cli.h cyton_diagnostic_serial.h bci_diagnostic.h bci_diagnostic_options.h
PARSER = ../../kernel/linux/drivers/xtos/bci/xtos_cyton_packet.h ../../kernel/linux/drivers/xtos/bci/xtos_cyton_config.h

.PHONY: all clean test
all: bci
bci: main.c bci_stream.c bci_stream.h cyton_serial.c cyton_serial.h bci_decode.h cli_options.h bci_config_cli.c bci_config_cli.h $(PARSER) $(RT_SRC) $(SIGNAL_SRC) $(BCI_SRC) $(BCI_HEADERS) $(CALIBRATION_SRC) $(CALIBRATION_HEADERS) $(DIAGNOSTIC_SRC) $(DIAGNOSTIC_HEADERS)
	$(CC) $(CPPFLAGS) $(CFLAGS) -o $@ main.c bci_stream.c cyton_serial.c bci_config_cli.c $(CALIBRATION_SRC) $(DIAGNOSTIC_SRC) $(RT_SRC) $(SIGNAL_SRC) $(BCI_SRC) $(LDFLAGS) $(LDLIBS)
test_bci_stream: test_bci_stream.c bci_stream.c bci_stream.h $(RT_SRC) $(SIGNAL_SRC)
	$(CC) $(CPPFLAGS) $(CFLAGS) -o $@ test_bci_stream.c bci_stream.c $(RT_SRC) $(SIGNAL_SRC) $(LDFLAGS) $(LDLIBS)
test_bci_decode: test_bci_decode.c bci_decode.h $(PARSER)
	$(CC) $(CFLAGS) -o $@ test_bci_decode.c $(LDFLAGS)
test_cli_options: test_cli_options.c cli_options.h
	$(CC) $(CFLAGS) -o $@ test_cli_options.c $(LDFLAGS)
test: all test_bci_stream test_bci_decode test_cli_options
	./test_bci_stream
	./test_bci_decode
	./test_cli_options
	python3 test_cyton_serial.py
	python3 ../../tests/test_bci_cli.py
clean:
	rm -f bci test_bci_stream test_bci_decode test_cli_options
