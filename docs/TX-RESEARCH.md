# Can the ESP32-S3 transmit? Research notes

Status: research, nothing here is part of the supported firmware. The narrowband build never transmits.

## Stage 0: what the vendor PHY library does for a TX tone (disassembly only, nothing measured)

Source: `libphy.a` and `librftest.a` of ESP-IDF 5.5 (`components/esp_phy/lib/esp32s3`), read with `objdump -dr`. Everything below is
read from instruction sequences and is **inferred**; field names are guesses until a measurement confirms them.

* `start_tx_tone_step(a, i, g, b, q, h)` (`phy_reg.o`) writes the frontend registers `0x60006040` (I side) and `0x60006044` (Q side):
  bits 9:0 a signed value (`i` or `q` shifted right by 2; the two low bits go to `0x60006050` bits 1:0 and 3:2 when bit 29 of
  `0x60006040` is set), bits 17:10 the negated byte `g` (or `h` for Q), bits 25:18 the byte `a` (or `b` for Q). If `a | b` is not
  zero, bit 26 of `0x60006000` is cleared and bit 10 of `0x600061e4` is set; otherwise the opposite. That pair of bits looks like
  the switch between normal baseband TX data and these constants. The registers hold constants, so I and Q are
  DC levels: the transmitter is a carrier at the LO, offset in phase and amplitude by (I, Q).
* `start_tx_tone` is a wrapper that scales `i` and `q` by 32/5 (10-bit mode) or 128/5 (12-bit mode) first.
* `phy_txtone_start(freq_mhz16, offset_s16, gain8)` (`phy_feature.o`): `set_rf_freq_offset` (→ `set_rfpll_freq(chan, freq, offset)`),
  `target_power_backoff(min(gain - 12, 127))`, sets bit 1 of `0x60006000`, writes a power field into bits 17:10 of it, calls two
  `g_phyFuns` hooks, then `start_tx_tone_step(1, 0, <gain byte>, 0, 0, 0)`: I = constant 1, Q = 0. So a "tone" is the LO with a
  constant baseband level of 1.
* `phy_set_freq(freq_mhz16, offset_s16)` also ends in `set_rfpll_freq`. This firmware already owns the PLL (`tune_pll()` in
  `esp32s3/src/radio.c`), so the transmit frequency would come from the same synthesizer plan as in receive.
* `force_iq_set` (`librftest.a`) writes `0x6000607C`, the receive I/Q correction register (see `board.h`). It is not the transmit path.
* `stop_tx_tone` clears bits in `0x60006040/44/4c` and sets bit 26 of `0x60006000` again.

What this suggests, to be tested: if the hardware samples these registers continuously, writing `0x60006040/44` from the CPU gives an
I/Q source of CPU speed, limited by the 8/10-bit fields and by the filters after them. A DC value gives a carrier; a sequence gives
modulation. Whether it works, at what rate and how clean, is unknown.

## Stage 2: an unmodulated carrier from our own firmware (measured)

Build: `make -C esp32s3 NARROWBAND=1 TXTEST=1 BUILD=build-tx` (research only, never part of a release). `radio_tx_test()` in `radio.c`
follows the PHY's own continuous-wave test (`wifiscwout`): `tune_pll()` to the wanted LO, `txcal_debuge_mode()`, `tune_pll()` again,
`start_tx_tone_step(1, 0, g, 0, 0, 0)`, wait, `start_tx_tone_step(0, ...)`, `txcal_work_mode()`, then the receiver is configured again. No
WLAN channel is involved: the carrier sits where the project's LO plan puts it. Limits in the firmware: 2320..2400 MHz, g >= 64, 5 s.
Receiver: PlutoSDR at 50 cm, centre 2349.5 MHz, 3 Msps, `host/python/espdr_txtest.py --freq-khz 2350000 --g 127 --ms 500 --go`.

| Result | |
|---|---|
| Carrier frequency | **2350.0125 MHz** for 2350.000 requested (+5.3 ppm, in line with this board's crystal), stable to the 0.1 kHz resolution of 10 ms windows |
| Burst | exactly 0.5 s, off immediately afterwards; the receiver returns to normal |
| Spurs | no line above 17 dB over the noise in the 3 MHz recorded |
| `g` | smaller means stronger: 127 → 124 → 120 gave +1.2 dB and +0.7 dB, about 0.27 dB per step (the PHY's 0.25 dB unit) |
| Level | PlutoSDR (not calibrated) at gain 0 dB: -60 dBFS at 50 cm; the AD9361's full scale is roughly -10 dBm there, so about -70 dBm received and about -35 dBm radiated, +-10 dB. At gain 60 dB the same carrier overloads the Pluto. |

Conclusion: the transmit chain works with our bring-up, on a frequency of our choosing.

## Stage 3: moving the carrier by writing the frontend registers (measured, negative so far)

Idea: `start_tx_tone_step(a, i, g, b, q, h)` writes `0x60006040` (I side) and `0x60006044` (Q side); if the fields were a baseband I/Q input,
calling it in a loop would modulate the carrier. Same setup as stage 2, `g = 127`, PlutoSDR at 0 dB gain.

| Test | What | Result |
|---|---|---|
| 3a | `(1, A cos, g, 0, A sin, 0)` as a rotating vector at +20 kHz, 100 kHz update rate, A = 400, 300 ms, no update late | carrier 35 dB **lower** than the plain carrier; weak lines at multiples of 20 kHz around it (+9 and +6 dB over the noise at -20/+20 kHz) |
| 3b/3c | the same with a 10 Hz rotation, A = 400 and 900 | essentially no signal (rms 0.00051 against a noise of 0.00044) |
| 4 | 10 static states, 60 ms each: `(a,i,b,q)` = (1,0,0,0) 0.771e-3; (0,0,0,0) 0.049; (0,100,0,0) 0.025; (0,-100,0,0) 0.003; (0,0,0,100) 0.013; (0,0,0,-100) 0.017; (1,100,0,0) 0.008; (1,-100,0,0) 0.003; (2,0,0,0) 0.009; (0,300,0,0) 0.010 (carrier magnitude, full scale 1) | **only the first state transmits**, 65 ms of activity in the recording; every later state, including (1,100,0,0) which differs from the first only in `i`, and (2,0,0,0), gives at least 25 dB less |

Reading: the registers are not a plain I/Q input, and a second call does not simply change the carrier. Either the fields mean something
else (a tone generator with `i` and `q` as frequency or phase terms, a power-up or gain sequence that only the first call performs), or the
first call arms something the later ones switch off. The next tests separate these.

