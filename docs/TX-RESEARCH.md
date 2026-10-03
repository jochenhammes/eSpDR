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

## Stage 1 onward
See the plan in the project notes: reference run with the ESP-IDF `cert_test` example, own transmit path, modulation tests with a PlutoSDR
as the receiver. Results will be added here with numbers.
