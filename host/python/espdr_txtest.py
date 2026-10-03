#!/usr/bin/env python3
"""RESEARCH ONLY: transmit from an ESP32-S3 running a `make NARROWBAND=1 TXTEST=1` firmware (see docs/TX-RESEARCH.md).

The release firmware never transmits. This tool needs the research build, loaded into RAM with espdr_load.py. Nothing is sent without --go.
The firmware limits the frequency to 2320..2400 MHz and the duration of every transmission; it is the 13 cm amateur band, and you are
responsible for holding the licence for whatever you transmit.

Modes (all need --freq-khz):
  unmodulated carrier          --g 127 --ms 500 --go
  FM voice from a WAV file     --audio speech.wav --fm-dev 2500 --go            (40 kHz updates, +-2.5 kHz, pre-emphasis; <= 4.9 s)
  SSB (USB) voice, polar       --ssb speech.wav --ms 2400 --carrier 0.05 --loops 8 --go
  SSB two-tone test            --ssb twotone:700,1700 --ms 300 --carrier 0.0 --go
  FM test tone / FSK           --fm-tone 1000 --fm-dev 3000 --go   /   --fsk-dev 5000 --rate 50 --go
The other options (--nco-hz, --ladder, --gain-ladder, --gain-square, --dump-regs, --backoff, --force-g) reproduce the measurements
in docs/TX-RESEARCH.md. `--selftest` checks the signal preparation without hardware.
"""
import argparse
import sys

import espdr_nb as nb

OP_DURATION, OP_CARRIER, OP_NCO_HZ, OP_NCO_RATE, OP_NCO_AMP, OP_NCO = 60, 61, 62, 63, 64, 65
OP_LADDER_CLEAR, OP_LADDER_ADD, OP_LADDER_RUN, OP_FSK, OP_FM = 66, 67, 68, 69, 70
OP_AUDIO_DATA, OP_AUDIO_CLEAR, OP_AUDIO_PLAY = 71, 72, 73
OP_GAIN, OP_REGS, OP_REGS_READ, OP_BACKOFF, OP_SSB = 74, 75, 76, 77, 78


AUDIO_BUFFER_BYTES = 3 * 65536   # TX_AUDIO_MAX in esp32s3/src/radio.h: capture banks 0..2


def upload(link, data):
    """Sends `data` (uint8 array) into the firmware's audio buffer in 16 KB pieces and checks every reply."""
    import numpy as np
    data = np.asarray(data, dtype=np.uint8)
    data = np.concatenate([data, np.zeros((-len(data)) % 4, dtype=np.uint8)])   # the firmware stores whole 32-bit words
    if len(data) > AUDIO_BUFFER_BYTES:
        raise SystemExit(f"{len(data)} bytes do not fit into the {AUDIO_BUFFER_BYTES} byte buffer (shorten the audio)")
    link.command(OP_AUDIO_CLEAR, 0)
    for off in range(0, len(data), 16384):
        chunk = data[off:off + 16384].tobytes()
        link.send(OP_AUDIO_DATA, len(chunk))
        link.ser.write(chunk)
        status, total = link._response(OP_AUDIO_DATA, timeout=10)
        if status != 0:
            raise SystemExit(f"the firmware refused the audio upload (status {status}) after {off} bytes")


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


# Gain code g -> amplitude relative to g = 127, measured with the Pluto (SSB stage A1, 3 codes per point)
GAIN_CURVE = [(127, 0.0), (124, 1.32), (121, 1.94), (118, 2.79), (115, 3.82), (112, 4.74), (109, 5.22), (106, 6.05), (103, 7.03),
              (100, 8.05), (97, 8.79), (94, 9.78), (91, 10.56), (88, 11.57), (85, 12.26), (82, 13.07), (79, 13.91), (76, 14.76),
              (73, 15.51), (70, 16.33), (67, 17.12), (64, 17.90)]


def prepare_ssb(source, rate, ms, carrier, delay, drift_hz=210.0, drift_tau=0.9):
    """(data, reference): interleaved (word delta int8, gain code uint8) per update at `rate`, for USB polar modulation with a
    carrier: z = c + analytic(audio); amplitude |z| through the gain field, frequency d(arg z)/dt through the PLL word."""
    import numpy as np
    from scipy.signal import hilbert, resample_poly, butter, sosfilt
    from math import gcd
    n8 = int(8000 * ms / 1000)
    if source.startswith("twotone:"):
        f1, f2 = (float(v) for v in source[8:].split(","))
        t = np.arange(n8) / 8000.0
        x = 0.5 * np.sin(2 * np.pi * f1 * t) + 0.5 * np.sin(2 * np.pi * f2 * t)
        x = x * np.minimum(1, np.minimum(np.arange(n8), n8 - np.arange(n8)) / 80.0)   # 10 ms fades
    else:
        import wave
        w = wave.open(source)
        ch, fs0 = w.getnchannels(), w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(float).reshape(-1, ch).mean(axis=1) / 32768.0
        g = gcd(8000, fs0)
        x = resample_poly(x, 8000 // g, fs0 // g)[:n8]
        x = sosfilt(butter(4, [300, 2700], btype="band", fs=8000, output="sos"), x)
        x = np.clip(x / np.percentile(abs(x), 99.5), -1, 1)
    xa = hilbert(x)                                         # x + j H{x}: positive frequencies only = upper sideband
    xa = xa / np.max(abs(xa))
    up = rate // 8000
    xa_fine = resample_poly(xa, up * 10, 1)                 # bandlimited: the interpolation is exact up to the filter
    xa = xa_fine[::10]
    c = carrier
    z = c + (1 - carrier) * xa
    zf = c + (1 - carrier) * xa_fine
    # the envelope may be shifted by a fraction of an update: gain path later by `delay` updates (can be fractional)
    sh = int(round(10 * delay))
    Af = abs(np.roll(zf, sh))
    A = Af[::10]; A = A / A.max()
    phi = np.unwrap(np.angle(z))
    f = np.diff(phi, prepend=phi[0]) * rate / (2 * np.pi)   # Hz
    # thermal drift of the transmitter after switch-on (measured: -190 Hz in 2.5 s, time constant 0.9 s, repeatable): cancel it
    tsec = np.arange(len(f)) / rate + 0.02            # the carrier has been on for 20 ms when the first update arrives
    f = f - drift_hz * np.exp(-tsec / drift_tau)
    STEP = 30e6 / 65536
    e1 = e2 = 0.0
    dw = np.zeros(len(f), dtype=np.int64)
    for i, v in enumerate(f / STEP):
        u = v - 2 * e1 + e2
        w = np.floor(u + 0.5)
        e2 = e1
        e1 = w - u
        dw[i] = w
    dw = np.clip(dw, -80, 80).astype(np.int8)        # the low byte of the word must not wrap (base 0x55: -85..+170)
    db = 20 * np.log10(np.maximum(A, 1e-6))                  # 0 dB at the peak
    top = GAIN_CURVE[-1][1]                                  # the peak sits at g = 64
    gcode = np.interp(db + top, [p[1] for p in GAIN_CURVE], [p[0] for p in GAIN_CURVE])
    gcode = np.clip(np.round(gcode), 64, 127).astype(np.uint8)
    data = np.empty(2 * len(dw), dtype=np.uint8)
    data[0::2] = dw.view(np.uint8)
    data[1::2] = gcode
    return data, (x, A)


def selftest():
    """Signal preparation against an ideal polar transmitter, no hardware: the SSB data must give a clean upper sideband."""
    import tempfile, wave, os
    import numpy as np
    ok = True
    STEP = 30e6 / 65536

    def check(name, good, detail=""):
        nonlocal ok
        ok &= bool(good)
        print(f"{name}: {'OK' if good else 'FAIL'} {detail}")

    for carrier in (0.55, 0.15, 0.0):
        data, _ = prepare_ssb("twotone:700,1700", 40000, 300, carrier, 1.0, 0.0)
        dw = data[0::2].view(np.int8).astype(float)
        g = data[1::2].astype(float)
        rate = 40000.0
        phase = 2 * np.pi * np.cumsum(dw * STEP) / rate
        db = np.interp(g, [p[0] for p in GAIN_CURVE][::-1], [p[1] for p in GAIN_CURVE][::-1])   # dB above g = 127
        amp = 10 ** (-(GAIN_CURVE[-1][1] - db) / 20)                                            # peak = 1
        amp = np.roll(amp, -1)          # the transmitter's frequency path is 1 update (25 us) earlier than its gain path: undo the delay of prepare_ssb()
        s = amp * np.exp(1j * phase)
        s = s[400:-400]
        sp = np.abs(np.fft.fftshift(np.fft.fft(s * np.hanning(len(s)))))
        f = np.fft.fftshift(np.fft.fftfreq(len(s), 1 / rate))
        line = lambda hz: sp[np.abs(f - hz) < 40].max()
        wanted = min(line(700), line(1700))
        mirror = max(line(-700), line(-1700))
        check(f"two-tone, carrier {carrier:.2f}: unwanted sideband", 20 * np.log10(wanted / mirror) > 25,
              f"{20 * np.log10(wanted / mirror):.1f} dB below the wanted tones")
        check(f"two-tone, carrier {carrier:.2f}: ranges", abs(dw).max() <= 80 and g.min() >= 64 and g.max() <= 127 and len(data) % 4 == 0,
              f"word delta {dw.min():.0f}..{dw.max():.0f}, gain code {g.min():.0f}..{g.max():.0f}")
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "t.wav")
        t = np.arange(44100 * 2) / 44100.0
        x = (0.3 * np.sin(2 * np.pi * 1000 * t) * 32767).astype("<i2")
        w = wave.open(path, "wb")
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(44100); w.writeframes(x.tobytes()); w.close()
        a = prepare_audio(path, 20000)
        check("FM audio: 2 s tone at 20 kHz", len(a) == 40000 and abs(a).max() == 127, f"{len(a)} samples, peak {abs(a).max()}")
        d, _ = prepare_ssb(path, 40000, 1500, 0.3, 1.0, 0.0)
        check("SSB from a WAV file", len(d) == 2 * 60000 and len(d) <= AUDIO_BUFFER_BYTES, f"{len(d) // 2} updates")
    print("SELFTEST", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-p", "--port", default="auto")
    ap.add_argument("--selftest", action="store_true", help="check the signal preparation, no hardware")
    ap.add_argument("--freq-khz", type=int)
    ap.add_argument("--g", type=int, default=127, help="test gain field: larger = weaker carrier (64..127)")
    ap.add_argument("--ms", type=int, default=500, help="duration of the carrier, 1..5000 ms")
    ap.add_argument("--nco-hz", type=int, help="move the carrier with an I/Q oscillator at this offset (stage 3 experiment)")
    ap.add_argument("--ladder", help="static register states 'a,i,b,q;a,i,b,q;...' (a,b 0..63, i,q -512..511), each held --hold ms")
    ap.add_argument("--fsk-dev", type=int, help="FSK through the PLL word: deviation in Hz (<= 20000); --rate is then the toggle rate in Hz (<= 5000)")
    ap.add_argument("--ssb", help="SSB (USB) by polar modulation: a WAV file, or 'twotone:700,1700' (Hz), for the length --ms (<= 2400)")
    ap.add_argument("--carrier", type=float, default=0.55, help="with --ssb: carrier amplitude as a fraction of the peak envelope (0.3..0.8)")
    ap.add_argument("--drift-hz", type=float, default=210.0, help="with --ssb: initial thermal frequency drift to cancel (0 = off); done in the firmware, time constant 32768/rate")
    ap.add_argument("--loops", type=int, default=1, help="with --ssb: play the prepared clip this many times in a row (total <= 30 s)")
    ap.add_argument("--force-g", type=int, help="with --ssb: debugging, send this gain code at every update (and no word deltas)")
    ap.add_argument("--ssb-delay", type=float, default=1.0, help="with --ssb: delay of the gain path against the frequency path, in updates (can be negative)")
    ap.add_argument("--dump-regs", action="store_true", help="SSB stage A: carrier on for 50 ms, print the frontend registers 0x60006000..0x60006060")
    ap.add_argument("--backoff", help="SSB stage A: 'B0,B1,STEP,HOLDMS': the PHY's target_power_backoff(b) for b = B0..B1 (0.25 dB units?) with the carrier on")
    ap.add_argument("--gain-ladder", help="SSB stage A1: 'A,B,STEP,HOLDMS': the carrier's gain code g runs from A to B (>= 127; larger = weaker)")
    ap.add_argument("--gain-square", help="SSB stage A2: 'A,B,RATE,MS': g alternates between A and B, RATE times a second, for MS ms")
    ap.add_argument("--audio", help="speech sender: FM-modulate this WAV file (mono-mixed, band-limited, pre-emphasised, clipped); --fm-dev is the full-scale deviation, --rate the update rate")
    ap.add_argument("--save-processed", help="with --audio: write the processed int8 samples here (for comparing with a recording)")
    ap.add_argument("--fm-tone", type=int, help="FM by a sine tone of this frequency in Hz; --fm-dev is the deviation, --rate the PLL word update rate")
    ap.add_argument("--fm-dev", type=int, default=3000)
    ap.add_argument("--fast", action="store_true", help="with --fsk-dev: write only the low byte of the PLL word per update")
    ap.add_argument("--hold", type=int, default=50)
    ap.add_argument("--rate", type=int, help="update rate in Hz: PLL word updates for --audio/--ssb/--fm-tone (default 40000 / 40000 / 20000), toggle rate for --fsk-dev (default 50), NCO rate for --nco-hz (default 100000)")
    ap.add_argument("--amp", type=int, default=400, help="NCO amplitude (0..1000)")
    ap.add_argument("--go", action="store_true", help="really transmit")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if args.freq_khz is None:
        ap.error("--freq-khz is required")
    if args.rate is None:
        args.rate = 40000 if (args.audio or args.ssb) else 20000 if args.fm_tone else 50 if args.fsk_dev else 100000
    if not args.go:
        print(f"would transmit {args.freq_khz / 1e3:.3f} MHz, g={args.g}, {args.ms} ms; add --go", file=sys.stderr)
        return 2
    link = nb.open_link(args.port)
    nb.check_firmware(link)
    link.command(OP_DURATION, args.ms)
    if args.ssb:
        data, ref = prepare_ssb(args.ssb, args.rate, args.ms, args.carrier, args.ssb_delay, 0.0)
        if args.force_g:
            data[1::2] = args.force_g
            data[0::2] = 0
        if args.save_processed:
            data.tofile(args.save_processed)
        upload(link, data)
        link.command(OP_NCO_RATE, args.rate)
        link.command(OP_NCO_HZ, args.loops)
        link.command(OP_NCO_AMP, int(round(args.drift_hz)) & 0xFFFFFFFF)
        link.send(OP_SSB, args.freq_khz)
        secs = len(data) / 2 / (args.rate) * args.loops
        status, late = link._response(OP_SSB, timeout=secs + 8)
        print(f"status {status} ({'ok' if status == 0 else 'failed'}); updates: {len(data) // 2} x {args.loops} loops ({secs:.2f} s), late: {late}")
    elif args.dump_regs:
        link.send(OP_REGS, args.freq_khz | args.g << 22)
        status, w16 = link._response(OP_REGS, timeout=8)
        print(f"status {status}")
        for i in range(25):
            _, v = link.command(OP_REGS_READ, i)
            print(f"  0x{0x60006000 + 4 * i:08X}: 0x{v:08X}")
    elif args.backoff:
        b0, b1, st, hold = (int(v) for v in args.backoff.split(","))
        link.command(OP_NCO_AMP, b0)
        link.command(OP_NCO_RATE, st & 0xFFFFFFFF)
        link.command(OP_NCO_HZ, b1 | hold << 16)
        link.send(OP_BACKOFF, args.freq_khz | args.g << 22)
        n = abs(b1 - b0) // max(abs(st), 1) + 1
        status, cyc = link._response(OP_BACKOFF, timeout=n * hold / 1000 + 8)
        print(f"status {status} ({'ok' if status == 0 else 'failed'}); one target_power_backoff() call took {cyc} CPU cycles ({cyc / 240:.1f} us)")
    elif args.gain_ladder or args.gain_square:
        mode = 0 if args.gain_ladder else 1
        a, b, c, d = (int(v) for v in (args.gain_ladder or args.gain_square).split(","))
        link.command(OP_NCO_AMP, b)
        link.command(OP_NCO_RATE, c & 0xFFFFFFFF)
        link.command(OP_NCO_HZ, d)
        link.send(OP_GAIN, args.freq_khz | mode << 22 | a << 24)
        status, writes = link._response(OP_GAIN, timeout=(d * (abs(b - a) // max(abs(c), 1) + 1) / 1000 if mode == 0 else d / 1000) + 8)
        print(f"status {status} ({'ok' if status == 0 else 'failed'}); gain writes: {writes}")
    elif args.audio:
        up = 2 if args.rate > 20000 else 1
        samples = prepare_audio(args.audio, args.rate // up)
        if args.save_processed:
            samples.tofile(args.save_processed)
        upload(link, samples.view('uint8'))
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
