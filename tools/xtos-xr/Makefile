CC ?= cc
BUILD_DIR ?= ../../.build/xr
CPPFLAGS += -D_POSIX_C_SOURCE=200809L -DXR_NO_PROTOTYPES -DXR_USE_TIMESPEC -Iinclude -Ivendor/openxr/include
CFLAGS ?= -O2
CFLAGS += -std=c11 -Wall -Wextra -Werror
LDLIBS += -ldl
SOURCES = src/runtime_adapter.c src/xr_probe.c
HEADERS = include/xtos_xr/runtime_adapter.h vendor/openxr/include/openxr/openxr.h vendor/openxr/include/openxr/openxr_platform.h vendor/openxr/include/openxr/openxr_platform_defines.h

.PHONY: all clean
all: $(BUILD_DIR)/xr-probe

$(BUILD_DIR)/xr-probe: $(SOURCES) $(HEADERS) Makefile
	mkdir -p $(BUILD_DIR)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(SOURCES) $(LDFLAGS) $(LDLIBS) -o $@

clean:
	rm -f $(BUILD_DIR)/xr-probe
