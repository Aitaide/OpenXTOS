# SPDX-License-Identifier: GPL-2.0

CC ?= cc
CFLAGS ?= -std=c11 -O2 -Wall -Wextra -Werror
TARGET := test_priority_queue

.PHONY: all test clean

all: $(TARGET)

$(TARGET): test_priority_queue.c ../../kernel/linux/include/linux/xtos_queue.h
	$(CC) $(CFLAGS) -I../../kernel/linux/include -o $@ test_priority_queue.c

test: $(TARGET)
	./$(TARGET)

clean:
	$(RM) $(TARGET)
