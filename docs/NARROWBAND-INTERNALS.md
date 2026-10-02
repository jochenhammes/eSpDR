# Narrowband mode: how it works inside

For users, see [NARROWBAND.md](NARROWBAND.md). This page is for people who want to change the firmware or understand
the numbers. The radio side (16 Msps dump, banks, link timing) is described in [RADIO.md](RADIO.md) and the [original README](../README-eSpDR.md);
here only what the narrowband mode adds.

## The time budget

At 16 Msps the radio writes 16 million I/Q pairs per second into four 16384-pair SRAM banks. A *capture unit* is what one bank
holds when it is switched, 15360 to 16288 pairs, about 1 ms. The two CPU cores take every other unit (lane 0 on core 0,
lane 1 on core 1, as in the FPGA design). A core therefore has two unit periods, **480 000 cycles at 240 MHz**, to
decimate its unit, build the packet, and be back at the polling loop before the next bank needs preparing. If it is late,
the run stops with failure code 7 or 8 (`bank still busy` / `next bank prepared too late`, both "DSP too slow").

## The decimator (`esp32s3/src/dsp.c`)

* **Stage 1**: a 4th-order CIC decimating by 16 (16 Msps to 1 Msps), evaluated as the FIR it is: kernel `(1+z^-1+…+z^-15)^4`,
  61 coefficients (largest 2736, sum 65536), padded to 64. Block *j* needs the 64 samples of blocks *j-3..j*, so a unit starts three blocks
  early with empty rings. For 10-bit input nothing wraps (|output| ≤ 2^25), so this is bit-for-bit what integrators and combs in 32-bit
  arithmetic give. The output is shifted right by 11 to 16 bits.
* **Stage 2**: a symmetric FIR (Q15) at 1 Msps, decimating by `R2` = 4 (250 ksps, 73 taps, ±100 kHz), 3 (333 ksps, 61 taps, ±133 kHz) or
  2 (500 ksps, 41 taps, ±200 kHz, not usable: see the limits in the guide). `tools/design_taps.py` designs them with a least-squares fit that
  also equalises the CIC passband droop, so the output is flat to the edge of the passband (R2 = 3: ±0.01 dB, stopband from 200 kHz
  at least 68 dB down, CIC images at least 65 dB down).
* **Units are independent.** Output sample *k* depends only on input pairs, never on earlier output, so each unit is decimated
  without state from the previous one: the CIC integrators start from zero `NCIC` blocks before the first block the FIR needs,
  which reproduces the true response exactly from there on, and the filter history (`taps - 1 + NCIC` blocks, about
  76 blocks of 16 pairs) is re-read from the previous bank. Every output belongs to exactly one unit (the one holding the last
  input pair that influences it), so `dsp_outputs()` can tell the next lane in advance how many samples this unit produces.
* **32-bit only.** Core 1 must not call into the ROM: captures overwrite the ROM's working memory in bank 3. That rules out
  division by non-powers of two, 64-bit arithmetic (libgcc helpers live in ROM), and any compiler-inserted `memcpy`/`memset`
  (`-fno-tree-loop-distribute-patterns`). The Makefile also fails the link if one of the firmware's functions would be shadowed by
  a ROM function of the same name.
* **One code copy per core.** `dsp.c` is compiled twice (`dsp_core1.o` with `-DDSP_ATTR=section(.core1_text)`), and
  `stream_unit()` exists as `stream_unit_core1()` too, so core 1 runs entirely from its own instruction bank and the two
  cores never contend for instruction fetches. `objdump` on the object files shows only calls inside `.core1_text` (and the
  `idle` hook, which is NULL on core 1).

### Why it is fast enough: where the cycles went

A first, plain C version needed **764 000 cycles per unit (159 % of the budget)**. Measured with `espdr_nb.py dspbench` and, in a
`PROFILE=1` build, per-section cycle counters:

| Change | Cycles per unit (R2=4) |
|---|---|
| first version | 764 000 (47.7 per pair) |
| stage-1 integrators in their own function (`cic_block`) so the eight accumulators stay in registers | 653 000 |
| FIR folded on its symmetry | 624 000 |
| history arrays static instead of on the stack, FIR in its own function | 551 000 (measured with the profiling counters, which add about 30 000) |
| FIR on the S3 SIMD unit | 399 000 (83 %) |
| CIC as a 64-tap SIMD FIR over unpacked samples (no integrators, no combs) | **288 000 (60 %)**, 62 % in live runs |

The surprise was the stack: with the history arrays in the function's 1.3 KB frame, every access beyond 510 bytes of offset costs
three instructions (`movi`, `add`, `l32i`), and the unrolled stage-1 loop spilled its eight integrators there. The
hardware is not slow: a micro-test measured one cycle per instruction and per load from both instruction banks and from both
data memories.

**The SIMD FIR.** `ee.vmulas.s16.accx` multiplies 8 signed 16-bit pairs and adds the sum into a 40-bit accumulator, so a 73-tap FIR
is ten vector steps per channel instead of 146 multiply-adds (eight for R2 = 3). The vector loads must be 16-byte aligned, but the history window
starts at an arbitrary sample. `design_taps.py` therefore also emits the taps eight times, shifted by 0 to 7 samples and zero
padded; the code picks the row that matches the window's offset and reads whole aligned vectors, the zeros cancelling the extra
samples at both ends. The result is bit-identical to the C version, which stays as the host reference: the chip checks it itself
(`dspbench` runs both paths on the same noise and reports the number of differing bytes, 0 for R2 2, 3 and 4, cs8 and cs16, both
core copies). Measured per unit: 285 000 cycles (59 %) at R2 = 4, 295 000 (61 %) at R2 = 3, 311 000 (65 %) at R2 = 2; in live runs the worst
unit takes 62 %, 65 % and 69 %.

## Booting from flash

The image is built to run from RAM after a download-mode load, and it also boots from flash, with ESP-IDF's standard second-stage bootloader in
front of it (`esp32s3/flash/`, written by `espdr_load.py --flash`). Three things make that work:

* **No code in the first 32 KB of instruction RAM.** The ROM and the bootloader keep `0x40370000-0x40377FFF` for the flash cache, so the
  narrowband link puts the exception vectors in core 1's bank (`0x4037C000`) instead of `0x40374000`. The Makefile derives that linker script
  from `memory.ld` with `sed`; the FPGA firmware keeps `memory.ld` and stays byte-identical.
* **An application descriptor.** The bootloader reads an `esp_app_desc_t` from the start of the first flash-mapped segment and checks its
  eFuse-revision limits. `src/app_desc.c` provides one (magic word, revision limits 0 to 9999).
* **Two placeholder flash segments.** The bootloader maps the application's read-only data (`0x3C000000`) and code (`0x42000020`) through the
  flash cache and asserts if there are none. The link adds 256 bytes of the former (holding the descriptor) and 16 bytes of the latter. Nothing
  uses them at run time.

Loading the same image into RAM with `load-ram` still works: the ROM writes the placeholder bytes into the (unmapped) cache windows, which is harmless.
When the image comes from flash, the bootloader has also enabled the flash cache and set up the MMU; the firmware does not touch either. Timing
is identical to the RAM start (`dspbench` and live runs give the same cycle counts).

### The SIMD CIC

`cic_block_pie()` unpacks the 16 words of a block (at any word alignment: `ee.ld.128.usar.ip` and `ee.src.q` realign them), shifts left and
arithmetic-right to sign-extend the two 10-bit fields (`ssr` sets the amount for `ee.vsl.32` and `ee.vsr.32`), packs the 32-bit lanes into
16-bit lanes with `ee.vunzip.16`, stores them twice in a ring of four blocks per channel (so that the newest four blocks are always one
contiguous, 16-byte aligned run), and runs eight `ee.vmulas.s16.accx` per channel over it. The semantics of the shift, unzip and realign
instructions were checked on the chip before relying on them. `dspbench` runs the plain C version (`cic_block_c()`) on the same noise and
reports the bytes that differ (0).

## Unit joins

The dump engine occasionally writes the last burst of a bank twice. The decimated stream does not care, but the capture logic checks that
the next unit starts exactly where the previous one ended, and fails the run (code 5, "unit start not found") when it does not. With the
SIMD CIC on core 1 that check began to fail within seconds. What was ruled out by bisecting the kernel (each variant run six to eight times):
the unpacking and the ring stores are innocent; the 32 dense 128-bit loads of the multiply-add part on core 1 are the trigger; it does not
depend on where the code or the data lives (core 0's bank, RTC fast memory), on any state the kernel leaves behind (SAR, ACCX, Q registers),
or on how long the DSP takes; spreading the loads out with filler instructions cut the failures from 6 of 6 to 1 of 8; the same kernel on core 0
is clean. The mechanism is not understood. The narrowband build therefore accepts a join that is up to 8 pairs early (62.5 ns per pair) and
counts it (`NB_STAT_SLIPS`); exact joins stay mandatory in the FPGA build. In practice there is exactly one such slip per run, after
which none occur: 8 runs of 20 s and 5 runs of 120 s had no lost samples and one slip each.

## The packet stream (`protocol/narrowband.h`, `esp32s3/src/stream.c`)

Each unit becomes one packet: a 20-byte header (magic `0xE5 0x5D`, format, R2, sample count, dropped-unit count, unit
sequence number, the index of its first output sample, shift, and a 16-bit checksum such that the ten halfwords add up to
`0xFFFF`), then the I/Q payload. Packets enter a 12 KB byte FIFO in sequence order (the lanes finish out of order and wait their
turn). Core 0 moves FIFO bytes into the USB Serial/JTAG endpoint in its polling loops and every 32 blocks inside the decimator.
A packet that does not fit is dropped whole, so the byte stream stays aligned; the host sees the gap in the sample index.

The same endpoint carries the control protocol, so a stray byte from the host stops a run (the firmware treats any received
byte as `ESP_STOP`). That is why nothing else may open the port while a run is going, and why ModemManager must be kept off it.

The USB Serial/JTAG endpoint takes 64-byte packets and, on the test host, 12 of them per millisecond: 0.77 MB/s. 500 ksps of int8
I/Q (1 MB/s) therefore does not fit, regardless of CPU time.

## Control operations

On top of `protocol/control.h`: `NB_SET_DECIM` (29, only 4 is supported in practice), `NB_SET_FORMAT` (30), `NB_SET_OUTSHIFT` (31),
`NB_BENCH` (32, USB throughput), `NB_DSPBENCH` (33: decimator cycles, `NB_DSP_VERIFY` for the SIMD check, `NB_DSP_PROFILE*` in
`PROFILE=1` builds). Status 32 and 33 report dropped units and the FIFO peak. They are numbered after `ESP_STAT_COUNT`; keep them there
if upstream adds statistics.

## Tests

| Test | What it proves | Hardware |
|---|---|---|
| `python esp32s3/tools/dsp_selftest.py` | `dsp.c` is bit-exact against an independent integer model for R2 2, 3 and 4, cs8/cs16, random unit lengths; random start phases of the block counter and the wrap of the pair index; passband flat, aliases at the predicted level | no |
| `python host/python/espdr_nb.py selftest` | packet parser against a simulated ESP: clean, with gaps, with stray bytes; bench | no |
| `python host/python/espdr_rtltcp.py selftest` | rtl_tcp server against a simulated ESP: header, tones at the radio's rate (250 and 333 ksps), 1.024 and 2.4 MS/s, DC removal, retune, reconnect | no |
| `python host/python/espdr_nb.py dspbench` | cycles per unit on the chip, SIMD (FIR and CIC) vs C | yes |
| `make -C esp32s3 NARROWBAND=1 PROFILE=1` + `dspbench --profile` | cycles per section (stage 1, comb and history, FIR) | yes |

Continuous integration runs the first three and builds both firmware images, and checks that the FPGA firmware is unchanged.

## The sideband

`I + jQ` is *LO minus RF* straight from the radio (see RADIO.md). The raw stream keeps that; `espdr_rtltcp.py` and `--convert cf32`
conjugate it. Verified by moving a carrier 20 kHz up and seeing the raw tone move 20 kHz down.

## Ratios that are not a power of two

R2 = 3 (333.3 ksps) needs three things a power of two does not:

* **Which blocks trigger stage 2.** The block counter `ja` (pair index / 16) must satisfy `ja mod 3 = 0` at a trigger. `dsp_geom()` gets
  `ja mod 3` without a division from the base-4 digit sum (`dsp_mod3()`, since 4 = 1 mod 3), and the block loop keeps a running `trigger`
  block number instead of masking. The number of outputs of a unit is `ceil(n / 3)` as `(n * 0xAAAB) >> 17`, valid for n < 2^16.
* **A wrap of the pair index that keeps the phase.** The index since the start of a run used to wrap at 2^32 (after 268 s), which is a
  multiple of every power of two but not of 3: the trigger phase would have jumped by one block. `dsp_advance()` wraps it at
  4294967232 = 192 · 22369621 instead, a multiple of 16 · 12, which keeps the phase for R2 = 2, 3 and 4. `dsp_selftest.py` runs units that
  straddle this wrap.
* **Nothing may divide:** core 1 cannot call the ROM's division (captures overwrite the ROM's data), so all of it is 32-bit multiplies,
  shifts and adds.

To add another ratio: a `DESIGNS` entry in `design_taps.py`, the ratio-to-filter selection in `dsp.c`, `NB_SET_DECIM` and the `dspbench`
check in `main.c`, `dsp_selftest.py`, and `dspbench`. Internal RAM is tight (the `.data` region is 8 KB): the two copies of `dsp.c`
(one per core) share their tap tables, the core 1 copy is compiled with `DSP_TAPS_EXTERN`.

## Tests

| Test | What it proves | Hardware |
|---|---|---|
| `python esp32s3/tools/dsp_selftest.py` | `dsp.c` is bit-exact against an independent integer model for R2 2, 3 and 4, cs8/cs16, random unit lengths; random start phases of the block counter and the wrap of the pair index; passband flat, aliases at the predicted level | no |
| `python host/python/espdr_nb.py selftest` | packet parser against a simulated ESP: clean, with gaps, with stray bytes; bench | no |
| `python host/python/espdr_rtltcp.py selftest` | rtl_tcp server against a simulated ESP: header, tones at the radio's rate (250 and 333 ksps), 1.024 and 2.4 MS/s, DC removal, retune, reconnect | no |
| `python host/python/espdr_nb.py dspbench` | cycles per unit on the chip, SIMD (FIR and CIC) vs C | yes |
| `make -C esp32s3 NARROWBAND=1 PROFILE=1` + `dspbench --profile` | cycles per section (stage 1, comb and history, FIR) | yes |

Continuous integration runs the first three and builds both firmware images, and checks that the FPGA firmware is unchanged.

## The sideband

`I + jQ` is *LO minus RF* straight from the radio (see RADIO.md). The raw stream keeps that; `espdr_rtltcp.py` and `--convert cf32`
conjugate it. Verified by moving a carrier 20 kHz up and seeing the raw tone move 20 kHz down.

## Adding a decimation ratio

The geometry in `dsp_geom()` and the trigger test in the block loop use `r2 - 1` as a mask, which assumes a power of two. A ratio such as 3
(333 ksps, which would also fit the USB limit) needs a modulo-free replacement for `skip` and for the "every R2-th block" test, a new tap
set in `design_taps.py` (`DESIGNS`), and care that nothing divides: core 1 cannot call the ROM's division. Add the ratio to `NB_SET_DECIM`
and the ratio-to-filter selection in `dsp.c`, extend `dsp_selftest.py`, and check `dspbench`.
