#!/usr/bin/env python3
"""RESEARCH ONLY: transmits an unmodulated test carrier from an ESP32-S3 running a TXTEST=1 build (docs/TX-RESEARCH.md).

  espdr_txtest.py --freq-khz 2350000 --g 127 --ms 500 --go

The firmware limits the frequency to 2320..2400 MHz, g to 64..127 (larger = weaker, about 0.27 dB per step) and the duration
to 5 s (2 s with --nco-hz). Nothing is
sent without --go. You are responsible for the licence conditions of whatever you transmit.
"""
import argparse
import sys

import espdr_nb as nb

OP_DURATION, OP_CARRIER, OP_NCO_HZ, OP_NCO_RATE, OP_NCO_AMP, OP_NCO = 60, 61, 62, 63, 64, 65
OP_LADDER_CLEAR, OP_LADDER_ADD, OP_LADDER_RUN, OP_FSK, OP_FM = 66, 67, 68, 69, 70
OP_AUDIO_DATA, OP_AUDIO_CLEAR, OP_AUDIO_PLAY = 71, 72, 73


def prepare_audio(path, rate):
    """WAV -> signed 8-bit samples at `rate`: mono, 300..3000 Hz band, +6 dB/octave pre-emphasis between 300 and 3000 Hz, hard
    limiter at the 99.5th percentile (127 = full deviation)."""
    import wave
    import numpy as np
    from scipy.signal import butter, resample_poly, sosfilt, bilinear, lfilter
    from math import gcd
    w = wave.open(path)
    ch, width, fs, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
    if width != 2:
        raise SystemExit("only 16-bit WAV files are supported")
    x = np.frombuffer(w.readframes(n), dtype="<i2").astype(np.float64).reshape(-1, ch).mean(axis=1) / 32768.0
    g = gcd(int(rate), int(fs))
    x = resample_poly(x, int(rate) // g, int(fs) // g)
    x = sosfilt(butter(4, [300, 3000], btype="band", fs=rate, output="sos"), x)
    b, a = bilinear([1 / (2 * np.pi * 300), 1], [1 / (2 * np.pi * 3000), 1], fs=rate)  # +20 dB from 300 to 3000 Hz
    x = lfilter(b, a, x)
    x = x / np.percentile(abs(x), 99.5)
    x = np.clip(x, -1, 1)
    return np.round(x * 127).astype(np.int8)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-p", "--port", default="auto")
    ap.add_argument("--freq-khz", type=int, required=True)
    ap.add_argument("--g", type=int, default=127, help="test gain field: larger = weaker carrier (64..127)")
    ap.add_argument("--ms", type=int, default=500, help="duration of the carrier, 1..5000 ms")
    ap.add_argument("--nco-hz", type=int, help="move the carrier with an I/Q oscillator at this offset (stage 3 experiment)")
    ap.add_argument("--ladder", help="static register states 'a,i,b,q;a,i,b,q;...' (a,b 0..63, i,q -512..511), each held --hold ms")
    ap.add_argument("--fsk-dev", type=int, help="FSK through the PLL word: deviation in Hz (<= 20000); --rate is then the toggle rate in Hz (<= 5000)")
    ap.add_argument("--audio", help="speech sender: FM-modulate this WAV file (mono-mixed, band-limited, pre-emphasised, clipped); --fm-dev is the full-scale deviation, --rate the update rate")
    ap.add_argument("--save-processed", help="with --audio: write the processed int8 samples here (for comparing with a recording)")
    ap.add_argument("--fm-tone", type=int, help="FM by a sine tone of this frequency in Hz; --fm-dev is the deviation, --rate the PLL word update rate")
    ap.add_argument("--fm-dev", type=int, default=3000)
    ap.add_argument("--fast", action="store_true", help="with --fsk-dev: write only the low byte of the PLL word per update")
    ap.add_argument("--hold", type=int, default=50)
    ap.add_argument("--rate", type=int, default=100000, help="NCO update rate in Hz (1000..1500000)")
    ap.add_argument("--amp", type=int, default=400, help="NCO amplitude (0..1000)")
    ap.add_argument("--go", action="store_true", help="really transmit")
    args = ap.parse_args()
    if not args.go:
        print(f"would transmit {args.freq_khz / 1e3:.3f} MHz, g={args.g}, {args.ms} ms; add --go", file=sys.stderr)
        return 2
    link = nb.open_link(args.port)
    nb.check_firmware(link)
    link.command(OP_DURATION, args.ms)
    if args.audio:
        up = 2 if args.rate > 20000 else 1
        samples = prepare_audio(args.audio, args.rate // up)
        if args.save_processed:
            samples.tofile(args.save_processed)
        link.command(OP_AUDIO_CLEAR, 0)
        for off in range(0, len(samples), 16384):
            chunk = samples[off:off + 16384].tobytes()
            link.send(OP_AUDIO_DATA, len(chunk))
            link.ser.write(chunk)
            link._response(OP_AUDIO_DATA, timeout=10)
        link.command(OP_NCO_RATE, args.rate)
        link.command(OP_NCO_AMP, args.fm_dev)
        link.command(OP_NCO_HZ, up)
        link.send(OP_AUDIO_PLAY, args.freq_khz | args.g << 22)
        status, played = link._response(OP_AUDIO_PLAY, timeout=len(samples) * up / args.rate + 8)
        print(f"status {status} ({'ok' if status == 0 else 'failed'}); updates played: {played} of {len(samples) * up} ({len(samples) * up / args.rate:.2f} s at {args.rate} Hz)")
    elif args.fm_tone:
        link.command(OP_NCO_HZ, args.fm_tone)
        link.command(OP_NCO_RATE, args.rate)
        link.command(OP_NCO_AMP, args.fm_dev)
        link.send(OP_FM, args.freq_khz | args.g << 22)
        status, upd = link._response(OP_FM, timeout=args.ms / 1000 + 6)
        print(f"status {status} ({'ok' if status == 0 else 'failed'}); PLL word updates: {upd}")
    elif args.fsk_dev:
        link.command(OP_NCO_HZ, args.fsk_dev)
        link.command(OP_NCO_RATE, args.rate)
        link.command(OP_NCO_AMP, 1 if args.fast else 0)
        link.send(OP_FSK, args.freq_khz | args.g << 22)
        status, upd = link._response(OP_FSK, timeout=args.ms / 1000 + 6)
        print(f"status {status} ({'ok' if status == 0 else 'failed'}); PLL word updates: {upd}")
    elif args.ladder:
        link.command(OP_LADDER_CLEAR, args.hold)
        for st in args.ladder.split(";"):
            a, i, b, q = (int(v) for v in st.split(","))
            link.command(OP_LADDER_ADD, (a & 63) | (b & 63) << 6 | (i & 0x3FF) << 12 | (q & 0x3FF) << 22)
        n = len(args.ladder.split(";"))
        link.send(OP_LADDER_RUN, args.freq_khz | args.g << 22)
        status, run = link._response(OP_LADDER_RUN, timeout=n * args.hold / 1000 + 6)
        print(f"status {status} ({'ok' if status == 0 else 'failed'}); states run: {run} of {n}")
    elif args.nco_hz is None:
        link.send(OP_CARRIER, args.freq_khz | args.g << 22)
        status, sdm = link._response(OP_CARRIER, timeout=args.ms / 1000 + 6)
        print(f"status {status} ({'ok' if status == 0 else 'failed'}); PLL sigma-delta word with the carrier on: 0x{sdm:06X}")
    else:
        link.command(OP_NCO_HZ, args.nco_hz)
        link.command(OP_NCO_RATE, args.rate)
        link.command(OP_NCO_AMP, args.amp)
        link.send(OP_NCO, args.freq_khz | args.g << 22)
        status, late = link._response(OP_NCO, timeout=args.ms / 1000 + 6)
        print(f"status {status} ({'ok' if status == 0 else 'failed'}); updates written late: {late} of {args.ms * args.rate // 1000}")
    return 0 if status == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
