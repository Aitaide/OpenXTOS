#!/usr/bin/env python3
"""Deterministic corpus generator + mutation engine for the XTOS fuzzers.

No libFuzzer/AFL on this host, so this implements a small structure-aware
mutation loop with fixed seeds: reproducible runs, no network, no deps.

Corpus domains:
  * csv rows   -> fuzz_csv_parse (row grammar: int , double , double)
  * byte blobs -> fuzz_openbci_packet (packet-oriented streams)

Usage:
  gen_corpus.py --out DIR --rounds N          generate + mutate into DIR
  gen_corpus.py --check DIR                   re-run harnesses over DIR (used
                                              by tests to verify corpus stays
                                              crash-free)
"""

import argparse
import hashlib
import random
import sys
from pathlib import Path

CSV_SEEDS = [
    b"1000000,1.5,-2.5\n",
    b"0,0,0\n",
    b"18446744073709551615, 2147483647 , -2147483648\n",
    b"4000000,1e0,-1e0\n",
    b"42 , 0.4999999 , -0.5\n",
    b"2147483647.4,2147483647,-2147483647.5\n",
    b"7,  nan , 1\n",
    b"7,  inf , 1\n",
    b"7, -inf , 1\n",
    b"  12  ,  3.25  ,  4.75  \n",
    b"12,3.25,4.75,extra\n",
    b"\x00\x01\x02,1,2\n",
    b"9999999999999999999999999999999999,1,2\n",
    b"0x10,1,2\n",
    b"-5,1,2\n",
    b",,",
    b"1,2",
    b"1,2,3,4",
]

PACKET_SEEDS = [
    bytes([0xA0]) + bytes(32),
    bytes([0xA0]) + bytes([0xFF] * 32),
    bytes([0xA0]) + bytes([0x80, 0x00, 0x00] * 8) + bytes(8),
    bytes([0xA0]) + bytes([0x7F, 0xFF, 0xFF] * 8) + bytes(8),
    b"\xa0" + b"\x00" * 32,
    bytes([0x00, 0xA0]) + bytes(31),
    bytes(range(64)),
    b"\xff" * 128,
    b"",
]

CSV_TOKENS = [
    b",", b" , ", b"..", b"e", b"E", b"+", b"-", b"0x", b"nan", b"inf",
    b"\x00", b"\t", b" ", b"18446744073709551615", b"2147483648",
    b"-2147483649", b"0.5", b".5", b"1e999", b"-1e999", b"0", b"\xff",
]

PACKET_TOKENS = [b"\xa0", b"\x00", b"\xff", b"\xc0", b"\x0f"]


def rng_for(seed: int) -> random.Random:
    return random.Random(seed)


def mutate_bytes(rnd: random.Random, data: bytes, tokens) -> bytes:
    buf = bytearray(data)
    if not buf:
        return bytes(tokens[rnd.randrange(len(tokens))])
    for _ in range(rnd.randrange(1, 6)):
        if not buf:
            buf.extend(tokens[rnd.randrange(len(tokens))])
        op = rnd.randrange(5)
        pos = rnd.randrange(len(buf))
        if op == 0:  # flip bit
            buf[pos] ^= 1 << rnd.randrange(8)
        elif op == 1:  # set token
            tok = tokens[rnd.randrange(len(tokens))]
            buf[pos:pos + 1] = tok
        elif op == 2:  # insert token
            tok = tokens[rnd.randrange(len(tokens))]
            buf[pos:pos] = tok
        elif op == 3:  # truncate
            del buf[pos:]
        else:  # duplicate region
            end = min(len(buf), pos + rnd.randrange(1, 17))
            buf[pos:pos] = buf[pos:end]
    return bytes(buf)


def splice(rnd: random.Random, a: bytes, b: bytes) -> bytes:
    if not a or not b:
        return a or b
    cut_a = rnd.randrange(len(a))
    cut_b = rnd.randrange(len(b))
    return a[:cut_a] + b[cut_b:]


def generate(out_dir: Path, rounds: int, seed: int) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_dir = out_dir / "csv"
    pkt_dir = out_dir / "packet"
    csv_dir.mkdir(exist_ok=True)
    pkt_dir.mkdir(exist_ok=True)

    state = {"csv": list(CSV_SEEDS), "packet": list(PACKET_SEEDS)}
    counts = {"csv": 0, "packet": 0, "crash_candidates": 0}

    # Write seed corpus
    for i, blob in enumerate(CSV_SEEDS):
        (csv_dir / f"seed-{i:03d}").write_bytes(blob)
        counts["csv"] += 1
    for i, blob in enumerate(PACKET_SEEDS):
        (pkt_dir / f"seed-{i:03d}").write_bytes(blob)
        counts["packet"] += 1

    rnd = rng_for(seed)
    for round_index in range(rounds):
        for domain, tokens in (("csv", CSV_TOKENS), ("packet", PACKET_TOKENS)):
            pool = state[domain]
            strategy = rnd.randrange(4)
            if strategy == 0 and pool:
                child = mutate_bytes(rnd, pool[rnd.randrange(len(pool))], tokens)
            elif strategy == 1 and len(pool) >= 2:
                child = splice(rnd, pool[rnd.randrange(len(pool))],
                               pool[rnd.randrange(len(pool))])
            elif strategy == 2 and domain == "csv":
                # grammar-ish: int , float , float with hostile numbers
                ints = ["0", "-1", "18446744073709551615", "99999999999999999999",
                        "2147483648", "0x10", " 7 "]
                floats = ["0", "-0.0", "nan", "inf", "-inf", "1e999",
                          "2147483647.5", "-2147483648.5", "0.5", "1e-400",
                          "  3.25  ", "0.000000000000000000001"]
                row = f"{rnd.choice(ints)} , {rnd.choice(floats)} , {rnd.choice(floats)}\n"
                child = row.encode()
            else:
                size = rnd.randrange(0, 96)
                child = bytes(rnd.randrange(256) for _ in range(size))

            digest = hashlib.sha256(child).hexdigest()[:16]
            path = (csv_dir if domain == "csv" else pkt_dir) / f"g{round_index:04d}-{digest}"
            path.write_bytes(child)
            pool.append(child)
            counts[domain] += 1
            if len(pool) > 512:
                del pool[:len(pool) - 512]

    return counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--rounds", type=int, default=200)
    parser.add_argument("--seed", type=int, default=20261002)
    parser.add_argument("--check", action="store_true",
                        help="only count corpus files, don't generate")
    args = parser.parse_args()

    if args.check:
        csv = list((args.out / "csv").glob("*")) if (args.out / "csv").exists() else []
        pkt = list((args.out / "packet").glob("*")) if (args.out / "packet").exists() else []
        print(f"corpus_files csv={len(csv)} packet={len(pkt)}")
        return 0

    counts = generate(args.out, args.rounds, args.seed)
    print(f"generated csv={counts['csv']} packet={counts['packet']} "
          f"rounds={args.rounds} seed={args.seed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
