CC ?= cc
CFLAGS ?= -std=c11 -O2 -Wall -Wextra -Werror
ROOT := $(abspath ../..)
BUILD_DIR ?= $(ROOT)/.build/xtos-eeg-stream
CPPFLAGS ?= -I$(ROOT)/sdk/libxtos-bci/include -I$(ROOT)/sdk/libxtos-signal/include
SDK_LIBS ?= $(ROOT)/sdk/libxtos-bci/libxtos-bci.a $(ROOT)/sdk/libxtos-signal/libxtos-signal.a
LDLIBS ?= -lm
PREFIX ?= /usr
OBJS := $(addprefix $(BUILD_DIR)/,main.o eeg_export.o eeg_stream.o)

.PHONY: all test install clean
all: $(BUILD_DIR)/eeg-stream

$(BUILD_DIR):
	mkdir -p $@

$(BUILD_DIR)/%.o: %.c eeg_export.h eeg_stream.h | $(BUILD_DIR)
	$(CC) $(CPPFLAGS) $(CFLAGS) -MMD -MP -MF $(@:.o=.d) -c $< -o $@

$(BUILD_DIR)/eeg-stream: $(OBJS) $(SDK_LIBS)
	$(CC) $(CFLAGS) $(LDFLAGS) -o $@ $(OBJS) $(SDK_LIBS) $(LDLIBS)

test:
	env XTOS_EXPORT_EVIDENCE="$(XTOS_EXPORT_TEST_EVIDENCE)" python3 $(ROOT)/tests/test_eeg_stream_export.py ExportTests.test_serializer_golden ExportTests.test_serializer_invalid_atomic ExportTests.test_main_complete ExportTests.test_options_no_open ExportTests.test_lifecycle_failures ExportTests.test_stdout_full ExportTests.test_cancel_preserves_window_and_all_barriers ExportTests.test_final_delayed_read_cannot_publish_after_duration ExportTests.test_delayed_feature_extraction_cannot_publish_after_duration ExportTests.test_cancel_during_features_blocks_new_window ExportTests.test_real_broken_pipe_still_stops_and_closes

install: all
	install -d $(DESTDIR)$(PREFIX)/bin
	install -m 0755 $(BUILD_DIR)/eeg-stream $(DESTDIR)$(PREFIX)/bin/eeg-stream

clean:
	rm -f $(OBJS) $(OBJS:.o=.d) $(BUILD_DIR)/eeg-stream

-include $(OBJS:.o=.d)
