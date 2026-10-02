# eSpDR narrowband: an SDR from one ESP32-S3 board

[![CI](https://github.com/jochenhammes/eSpDR/actions/workflows/ci.yml/badge.svg?branch=narrowband)](https://github.com/jochenhammes/eSpDR/actions/workflows/ci.yml)
[![License: 0BSD](https://img.shields.io/badge/license-0BSD-blue.svg)](LICENSE)

![SDR++ receiving a PlutoSDR transmission through the ESP32-S3](docs/images/narrowband-sdrpp.png)

*SDR++ receiving a PlutoSDR transmission at 2.4307 GHz, through an ESP32-S3 dev board and a USB cable. The test transmitter draws text
into the waterfall.*

Every ESP32-S3 has a 2.4 GHz Wi-Fi receiver, and somewhere inside it is an I/Q stream that Espressif does not document.
[eSpDR](https://github.com/h0m3us3r/eSpDR) by h0m3us3r found how to read it, at up to 80 Msps, with an FPGA to carry the data out.
This fork adds a mode that **does not need the FPGA**: the chip decimates the stream itself and sends 250 ksps over its USB port, which is
all a normal SDR session needs. A small bridge makes the board look like an `rtl_tcp` server, so **SDR++** and other programs can use it as a
receiver for 1.84 to 2.79 GHz: the 13 cm amateur band, QO-100 uplink, ISM, Wi-Fi channels (best sensitivity around 2.4 GHz).

| | |
|---|---|
| What you need | one ESP32-S3 board with two USB ports, a USB cable (two to load), a PC with Python |
| Frequency range | 1.84 to 2.79 GHz (best sensitivity around 2.4 GHz) |
| Bandwidth | 250 ksps complex, about ±100 kHz usable |
| Format | int8 I/Q, 0.5 MB/s over USB |
| Works with | SDR++, anything that speaks `rtl_tcp`, GNU Radio (TCP), plain files |
| Direction | receive only |
| Status | works on the one board tested; reports from other boards wanted |

```
ESP32-S3 ADC  --16 Msps-->  CIC /16  -->  FIR /4  -->  250 ksps int8 I/Q  --USB-->  rtl_tcp bridge  -->  SDR++
              (both cores alternate between capture units; the FIR runs on the S3's SIMD unit)
```

## Quick start

You need a Linux PC, Python 3.9+, and an ESP32-S3 board with **both** USB ports connected (the UART port loads the firmware, the native
port carries the data). Details, other options and troubleshooting are in the [full guide](docs/NARROWBAND.md).

```sh
git clone https://github.com/jochenhammes/eSpDR.git && cd eSpDR
python3 -m venv ~/.espdr-venv && . ~/.espdr-venv/bin/activate
pip install -r host/python/requirements.txt
```

Download `iq-source-nb.bin` and `SHA256SUMS` from the [latest release](https://github.com/jochenhammes/eSpDR/releases/latest), then:

```sh
sha256sum -c SHA256SUMS --ignore-missing
sudo usermod -aG dialout "$USER"        # once; log out and in afterwards
sudo cp host/udev/70-espdr.rules /etc/udev/rules.d/ && sudo udevadm control --reload && sudo udevadm trigger   # once
python host/python/espdr_load.py iq-source-nb.bin       # loads it into the board's RAM; nothing is written to flash
python host/python/espdr_nb.py bench --seconds 3        # about 0.87 MB/s and 0 bad blocks means the link is fine
python host/python/espdr_rtltcp.py                      # leave it running
```

In SDR++ choose the source **RTL-TCP**, host `127.0.0.1`, port `1234`, press play, and tune somewhere between 1842 and 2790 MHz.
The firmware lives in the board's RAM, so load it again after every power cycle.

To record instead: `python host/python/espdr_nb.py run --freq 2412e6 --seconds 5 -o test.cs8`.
For GNU Radio, pluto-advanced-rx, the crystal correction (`--ppm`), gain and building the firmware yourself, see the [guide](docs/NARROWBAND.md).

## Good to know

* **A strong line at exactly 2400.000 and 2440.000 MHz** is the board's own 40 MHz crystal (its 60th and 61st harmonic), not a signal.
* **Fixed 250 ksps.** A 500 ksps mode exists in the firmware but needs more CPU time and USB bandwidth than the chip and USB Full-Speed
  provide, so it is not supported.
* **No calibrated levels, no AGC.** The gain is a table index; the crystal is within ±10 ppm, which is ±24 kHz at 2.4 GHz, so calibrate
  it against a known carrier.
* **One board tested** (a generic ESP32-S3-WROOM-1 dev board with two USB-C ports, on Ubuntu). Please tell us what you try:
  [board / hardware report](https://github.com/jochenhammes/eSpDR/issues/new?template=hardware_report.md).
* The measured numbers are in the [guide](docs/NARROWBAND.md#measured-performance).

## Documentation

| | |
|---|---|
| [docs/NARROWBAND.md](docs/NARROWBAND.md) | the user guide: install (prebuilt image or from source), SDR++ and GNU Radio, gain, ppm, performance, limits, troubleshooting |
| [docs/NARROWBAND-INTERNALS.md](docs/NARROWBAND-INTERNALS.md) | how it works: the time budget, the decimator, where the CPU cycles went, the packet format, the tests |
| [README-eSpDR.md](README-eSpDR.md) | the original eSpDR README: the 80 Msps design with the FPGA |
| [CONTRIBUTING.md](CONTRIBUTING.md) | how to help |

## About this project

This repository is a fork of [h0m3us3r/eSpDR](https://github.com/h0m3us3r/eSpDR), which is where the reverse-engineered radio dump path
and the 80 Msps FPGA design come from; the full design still builds from here (`make -C esp32s3` produces the original firmware,
byte-identical to upstream, which CI checks). The narrowband mode, the host tools and the documentation were developed by Jochen Hammes
with Claude (Anthropic) as the coding assistant, in the same spirit as the original: a technical proof of concept and a starting point
for tinkering. What the documentation calls measured was measured on real hardware.

The firmware reads the output of a Wi-Fi receiver in a way Espressif did not intend and does not document. This project is not
affiliated with or endorsed by Espressif, and it only receives. Receiving rules differ by country; you are responsible for your use.

## License

Original eSpDR code and the additions in this fork are released under the [Zero-Clause BSD license (0BSD)](LICENSE). Third-party code and
dependencies retain their licenses, including [Espressif's Apache-2.0 PHY library](https://github.com/espressif/esp-phy-lib) linked into the
firmware.
