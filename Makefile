UCX_DIR ?= /usr
CC      ?= gcc
CFLAGS   = -O2 -std=gnu11 -Wall -I$(UCX_DIR)/include
LDFLAGS  = -L$(UCX_DIR)/lib -lucp -luct -lucs -lucm -lpthread -lrt

.PHONY: all clean

all: bw_probe

bw_probe: src/bw_probe.c
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

clean:
	rm -f bw_probe
