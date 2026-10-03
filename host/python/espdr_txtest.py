#!/usr/bin/env python3
"""RESEARCH ONLY: transmits an unmodulated test carrier from an ESP32-S3 running a TXTEST=1 build (docs/TX-RESEARCH.md).

  espdr_txtest.py --freq-khz 2350000 --g 127 --ms 500 --go

The firmware limits the frequency to 2320..2400 MHz, g to 64..127 (larger = weaker) and the duration to 5 s. Nothing is
sent without --go. You are responsible for the licence conditions of whatever you transmit.
"""
import argparse
import sys

import espdr_nb as nb

OP_DURATION, OP_CARRIER = 60, 61


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-p", "--port", default="auto")
    ap.add_argument("--freq-khz", type=int, required=True)
    ap.add_argument("--g", type=int, default=127, help="test gain field: larger = weaker carrier (64..127)")
    ap.add_argument("--ms", type=int, default=500, help="duration of the carrier, 1..5000 ms")
    ap.add_argument("--go", action="store_true", help="really transmit")
    args = ap.parse_args()
    if not args.go:
        print(f"would transmit {args.freq_khz / 1e3:.3f} MHz, g={args.g}, {args.ms} ms; add --go", file=sys.stderr)
        return 2
    link = nb.open_link(args.port)
    nb.check_firmware(link)
    link.command(OP_DURATION, args.ms)
    link.send(OP_CARRIER, args.freq_khz | args.g << 22)
    status, sdm = link._response(OP_CARRIER, timeout=args.ms / 1000 + 6)
    print(f"status {status} ({'ok' if status == 0 else 'failed'}); PLL sigma-delta word with the carrier on: 0x{sdm:06X}")
    return 0 if status == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
