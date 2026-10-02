# Narrowband mode: an ESP32-S3 SDR with no FPGA

A 250 ksps, 2.4 GHz software-defined receiver that needs **one ESP32-S3 board and a USB cable**. The ESP32-S3
samples the I/Q output of its own Wi-Fi receiver at 16 Msps, decimates it on the chip, and streams the result
over USB. A small bridge makes it look like an `rtl_tcp` server, so **SDR++** and other programs can use it as a
receiver.

```
ESP32-S3 ADC  --16 Msps-->  CIC /16  -->  FIR /4  -->  250 ksps int8 I/Q  --USB-->  host  -->  SDR++ / GNU Radio / file
              (both cores alternate between capture units; the FIR runs on the S3's SIMD unit)
```

![SDR++ receiving a transmission from a PlutoSDR through the ESP32-S3: source RTL-TCP, 250 kHz, the text is drawn into the waterfall by the transmitter](images/narrowband-sdrpp.png)

*SDR++ receiving a PlutoSDR transmission at 2.4307 GHz through the ESP32-S3 and the `rtl_tcp` bridge. The test transmitter draws
text into the waterfall.*

This is the same ESP32-S3 firmware base as the 80 Msps FPGA design in the main [README](../README.md); the FPGA is only
needed for the full 80 MHz span. For a few hundred kHz it is not: 250 ksps of 8-bit samples is 0.5 MB/s, which USB
Full-Speed carries.

**What you get**

| | |
|---|---|
| Frequency range | 1.84 to 2.79 GHz (the Wi-Fi receiver's tuning range, including eSpDR's 5/6 conversion mode below 2.21 GHz) |
| Sample rate | 250 ksps complex, about ±100 kHz usable (flat to 0.04 dB; aliases at least 70 dB down) |
| Sample format | int8 I/Q (or int16), 0.5 MB/s (1.0 MB/s) over USB |
| Retuning | about 50 ms of silence |
| Direction | receive only |

**What you do not get**: a wide span (use the FPGA design for that), a calibrated absolute level, an AGC, or a
tested guarantee on boards other than the one below. The 2.4 GHz front end is a Wi-Fi receiver, so expect Wi-Fi-class
sensitivity and a strong spur at exactly 2400.000 and 2440.000 MHz (see [Known limitations](#known-limitations)).

Tested on: a generic *ESP32-S3-WROOM-1 development board with two USB-C ports* (a CH343 USB-UART bridge plus the
native USB port; chip revision v0.2, 8 MB PSRAM) connected to an x86-64 Ubuntu host, with a PlutoSDR+ as the test
transmitter. Other boards and hosts are untested; please
[tell us](https://github.com/jochenhammes/eSpDR/issues/new?template=hardware_report.md) what you tried.

## Requirements

* An ESP32-S3 board with **two USB ports** (UART bridge + native USB). This is what lets the loader reset the chip
  without buttons. A board with only the native port should work too (`espdr_load.py --native`, with BOOT and RESET pressed by hand
  each time), but that has not been tried.
* Linux (tested). The Python tools use `pyserial` and should run elsewhere, but nothing else has been tried.
* Python 3.9 or newer.
* Optional: an antenna (the board's PCB antenna is enough for strong nearby signals), SDR++ or GNU Radio.

## Installation

### Option A: prebuilt image (recommended)

1. Get the tools and install the Python packages:

   ```sh
   git clone https://github.com/jochenhammes/eSpDR.git
   cd eSpDR
   python3 -m venv ~/.espdr-venv && . ~/.espdr-venv/bin/activate
   pip install -r host/python/requirements.txt
   ```

2. Download `iq-source-nb.bin` and `SHA256SUMS` from the
   [latest release](https://github.com/jochenhammes/eSpDR/releases) and check the file:

   ```sh
   sha256sum -c SHA256SUMS --ignore-missing
   ```

3. Allow your user to open the ports. Either join the `dialout` group (then log out and in) or, for the native port
   only, install the udev rule, which also keeps ModemManager from probing the port (any byte it sends would stop a run):

   ```sh
   sudo usermod -aG dialout "$USER"
   sudo cp host/udev/70-espdr.rules /etc/udev/rules.d/ && sudo udevadm control --reload && sudo udevadm trigger
   ```

4. Connect **both** USB cables of the board, then load the image:

   ```sh
   python host/python/espdr_load.py iq-source-nb.bin
   ```

   The image runs from RAM and is lost at power-off; load it again after every power cycle (or make the board start it by itself,
   see [below](#optional-start-by-itself-at-power-up)). By default nothing is written to flash, so you cannot brick the board with it. On success the script prints the receiver's port (the board's
   native USB port, USB id `303a:1001`, for example `/dev/ttyACM1`).

5. Check the link:

   ```sh
   python host/python/espdr_nb.py bench --seconds 3
   ```

   On the test host this gives 0.87 MB/s and `0 bad blocks`. Narrowband mode needs 0.5 MB/s, so anything above that with no bad
   blocks is fine.

### Option B: build the firmware yourself

You need ESP-IDF **v5.5.3 or newer** in the 5.5 series for its headers, ROM linker scripts and Xtensa toolchain, about 1 GB
(v5.5.0 to v5.5.2 lack a clock-gating function the radio code calls, and fail with *implicit declaration of function
`periph_ll_phy_calibration_module_enable_clk_clear_rst`*):

```sh
git clone --depth 1 -b v5.5.5 https://github.com/espressif/esp-idf.git ~/esp-idf
cd ~/esp-idf && git submodule update --init --depth 1 components/esp_phy/lib
./install.sh esp32s3 && . ./export.sh
cd /path/to/eSpDR
make -C esp32s3 NARROWBAND=1 ESPTOOL="python -m esptool"      # -> esp32s3/build-nb/iq-source.bin
python host/python/espdr_load.py esp32s3/build-nb/iq-source.bin
```

`make -C esp32s3` without `NARROWBAND=1` still builds the original FPGA firmware, byte-identical to before. The build
directories differ (`build` and `build-nb`), but if you change flags, remove the directory first: `make` does not
notice flag changes.

## Optional: start by itself at power-up

If you want the board to be a receiver as soon as it gets power, write the image to its flash instead of its RAM:

```sh
python host/python/espdr_load.py --flash iq-source-nb.bin
```

This **overwrites the board's flash** (bootloader at `0x0`, partition table at `0x8000`, the image at `0x10000`), so whatever program was
there before is gone, and the script asks before it writes. From then on the board boots straight into the receiver; you only start
`espdr_rtltcp.py` (or SDR++ through it). To go back to RAM-only use, erase the flash (`python -m esptool erase-flash`): the board then waits
in its ROM loader and `espdr_load.py` works as before. How it works, and its limits (tested with quad-flash boards), are in
[esp32s3/flash/README.md](../esp32s3/flash/README.md).

## First run

```sh
python host/python/espdr_nb.py run --freq 2412e6 --seconds 5 -o test.cs8
```

records 5 seconds at 2412 MHz into `test.cs8` (interleaved int8 I, Q at 250 ksps) and prints how long the signal
processing needed (`worst unit service time`, a percentage of the time available; it must stay below 100 %).
`python host/python/espdr_nb.py -h` lists every option; the useful ones are `--gain` (default 60, see below),
`--format cs16` and `--convert cf32 --tcp 7373` (complex float over TCP for GNU Radio).

The port is found by its USB id. If you have several boards, name one with `-p /dev/ttyACMx`.

## Using it with other programs

### SDR++ (and anything that speaks rtl_tcp)

Start the bridge and leave it running:

```sh
python host/python/espdr_rtltcp.py --ppm 0
```

In SDR++ choose the source **RTL-TCP**, host `127.0.0.1`, port `1234`, and press play. Tune between 1842 and 2790 MHz.
Any sample rate SDR++ offers works: the bridge interpolates the 250 ksps stream to the rate the client asks for
(250 kS/s itself is passed through unchanged). The signal is still only about 200 kHz wide, whatever rate is shown.

What the bridge does: the radio runs only while a client is connected; a frequency, gain or ppm change stops the run,
reconfigures the radio and starts it again (about 50 ms of silence); the spectrum is conjugated, because the radio
delivers *LO minus RF*, so that a higher RF is a higher frequency; the DC offset is removed; the client's gain
(0 to 49.6 dB) is mapped to the ESP's gain selector 30 to 80 (`--gain-min`, `--gain-max`); frequencies outside the
ESP's range are clamped and logged, and a frequency the PLL cannot lock keeps the previous one. It serves one client at a time, on `127.0.0.1` unless you pass `--listen`. There is
no authentication, so do not expose it to a network you do not trust.

Other rtl_tcp clients work the same way; for example in *pluto-advanced-rx* choose the device "RTL-SDR" and enter
`127.0.0.1:1234` as its connection. (That program limits RTL-SDR devices to 24 to 1764 MHz; raise `FREQUENCY_RANGE_HZ`
in its `devices/rtlsdr.py`.)

### GNU Radio

Without `gr-osmosdr`, use the plain TCP stream. In one terminal:

```sh
python host/python/espdr_nb.py run --freq 2412e6 --convert cf32 --tcp 7373
```

and in the flowgraph a **TCP Source** block (address `127.0.0.1`, port `7373`, mode *Client*, type *Complex*,
sample rate 250000). Retuning needs a restart of the command. If `gr-osmosdr` is installed, its `rtl_tcp=127.0.0.1:1234`
source works with the bridge.

### Files

`--format cs8` (default) writes interleaved int8 I, Q; `cs16` int16. Raw files keep the radio's convention, *LO minus RF*:
a signal above the LO appears at a negative frequency. `--convert cf32` conjugates it to the usual convention
(`--native-iq` turns that off).

## Gain and level

The `--gain` selector (0 to 127) is a table index, not dB: roughly 1 dB per step from 35 to 76, and values above 83 repeat the
low end. The default 60 puts the noise at about 20 counts of the int8 samples; 24 leaves only about 2 counts, which is mostly
quantization noise. If strong signals clip (`max |I|` near 127), lower the gain or raise `--shift` (default 4).

With a CW carrier at 2400.05 MHz from a PlutoSDR, the received tone stepped 50.8, 39.8 and 29.7 dB above the noise for
TX attenuations of 30, 40 and 50 dB: linear over at least 20 dB. There is no absolute calibration.

## Frequency accuracy

The 40 MHz crystal of a typical board is within ±10 ppm, which is ±24 kHz at 2.4 GHz. Measure yours against a known
carrier and correct it with `--ppm`, positive if the ESP runs fast:

```sh
python host/python/espdr_rtltcp.py --ppm 5.5
```

Against a PlutoSDR+ carrier at 2400.050 MHz, `--ppm 5.5` moved the tone from +36 797 Hz to +49 979 Hz. That includes the
Pluto's own error, so calibrate against a source you trust. The bridge also removes the sub-step rounding of the ESP's
synthesizer (up to ±190 Hz) digitally.

## Measured performance

| | |
|---|---|
| USB throughput (bench, one xHCI host) | 0.87 MB/s sustained (13.6 packets of 64 bytes per ms), 0 bad blocks; the chip waits 95 % of the time for the host |
| Narrowband stream | 0.50 MB/s at cs8 |
| Signal processing per capture unit | 288 000 of 480 000 CPU cycles (60 %) in the on-chip test; worst unit in live runs 62 % |
| 120 s runs, 250 ksps | 30 000 3xx samples (the end of a unit), none lost; one unit join a few pairs off per run is counted and accepted |
| 2 min through the bridge at 2.4 MS/s | no dropped blocks |
| Retune | about 50 ms |
| Filter (host test, bit-exact against an integer model) | passband flat to 0.04 dB; aliases at least 71 dB down |

## Known limitations

* **Fixed 250 ksps.** The firmware also contains a 500 ksps mode (`--decim 2`), but it needs more CPU time and more USB
  bandwidth than the chip and USB Full-Speed link provide, and it stops with `DSP too slow`. Do not use it.
* **A strong spur at 2400.000 and 2440.000 MHz.** The 60th and 61st harmonics of the 40 MHz crystal are inside the
  receive band; with the LO at 2400 MHz a line sits at the centre, about 10 dB stronger than a −40 dBm carrier at gain 60. Do not
  mistake it for a signal. Faint lines at about ±1.6 kHz and ±9 kHz around strong signals were also seen; their origin is not
  investigated.
* **Sensitivity falls off away from 2.4 GHz.** The board's antenna and matching are made for Wi-Fi. At 1.9 GHz, which works through
  eSpDR's 5/6 conversion mode, a test carrier needed about 40 dB more power than at 2.4 GHz for the same signal-to-noise ratio.
* **The edges of the tuning range depend on the board.** The PLL has to lock at each request. On the test board it did not lock below
  1848 MHz or between about 2210 and 2219 MHz (the switch between the two conversion modes); every other frequency up to 2790 MHz worked.
  The tools report such a request (`status 6`), and the bridge then stays on the last frequency that worked and says so.
* **RX only.** The firmware never transmits.
* **One board tested.** Please report others, with `lsusb` and the output of `bench`.
* **Image rejection** (I/Q balance of the Wi-Fi front end) is not measured.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `no ESP32-S3 (USB id 303a:1001) found` | The image is not loaded, or the native USB port is not connected. Load it with `espdr_load.py`; connect both cables. |
| `espdr_load.py`: *no USB-UART bridge found* | Only the native port is connected, or your board has no bridge. Use `--native`, hold BOOT, tap RESET first. |
| `No serial data received` / *Failed to connect* while loading | Wrong port, a program holds it (a running bridge or SDR++?), or the cable is power-only. |
| The firmware stops answering after you open the port yourself | Do not clear DTR and RTS one after the other on the native port: DTR=0 with RTS=1 is its reset sequence and sends the chip back to the ROM loader. The tools here leave the lines alone. |
| Runs end after a second with `RUN FAILED` | Code 7 or 8 means the signal processing was too slow, code 2 a late poll. Use the supported 250 ksps; see [internals](NARROWBAND-INTERNALS.md). |
| `op 20 arg ...: status 2` | The frequency is outside 1841.666667 to 2790 MHz. |
| `WARNING: ... samples lost` | USB too slow for this host or hub. Try another port directly on the machine, or `--format cs8`. |
| After `--flash` the board reboots in a loop (the UART shows `ets_loader.c` or `abort()`) | The image must come from this tree's `make NARROWBAND=1` or a release of at least 0.1.1; older images lack the descriptor the bootloader needs. Fix by loading a good image with `--flash`, or erase the flash. |
| ModemManager sends bytes to the port | Install `host/udev/70-espdr.rules` (step 3 above). |
| Port names change (`ttyACM1`, `ttyACM2`, ...) | They depend on plug order. `espdr_load.py` and the tools find the right ones by USB id. |
| SDR++ shows a mirrored spectrum | Use the bridge (it conjugates) or `--convert cf32`; raw files keep the radio's *LO minus RF*. |

## Safety and legal

The firmware only receives. The board's radio is a 2.4 GHz Wi-Fi receiver used outside its intended purpose; the project is
not affiliated with or endorsed by Espressif. Receiving rules differ by country; you are responsible for your use.

## Credits

Built on [eSpDR](https://github.com/h0m3us3r/eSpDR) by h0m3us3r, whose reverse-engineered radio dump path and 16 Msps capture
this mode reuses. The narrowband mode, host tools and this documentation were developed by Jochen Hammes with Claude
(Anthropic) as the coding assistant, on the hardware described above. Everything stated here as measured was measured.

For how it works inside, and for developers: [NARROWBAND-INTERNALS.md](NARROWBAND-INTERNALS.md).
