CC ?= cc
CFLAGS ?= -O2 -Wall -Wextra -Werror -std=c11
OUT ?= ../../.build/xtos-sched-bench/periodic

.PHONY: all clean test
all: $(OUT)

$(OUT): periodic.c
	mkdir -p "$(dir $@)"
	$(CC) $(CFLAGS) -o "$@" "$<"

test:
	PYTHONDONTWRITEBYTECODE=1 python3 ../../tests/test_sched_periodic.py

clean:
	rm -f "$(OUT)"
