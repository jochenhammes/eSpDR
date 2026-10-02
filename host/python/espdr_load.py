#!/usr/bin/env python3
"""Loads the narrowband firmware image into the ESP32-S3's RAM and checks that it answers.

  espdr_load.py iq-source-nb.bin                    # finds the board's USB-UART bridge itself
  espdr_load.py iq-source-nb.bin --port /dev/ttyACM2   # name the bridge port yourself
  espdr_load.py iq-source-nb.bin --native           # board with only the native USB port: you press BOOT + RESET
  espdr_load.py --flash iq-source-nb.bin            # make the board start the receiver by itself (overwrites the flash)

By default the image runs from RAM, so it must be loaded again after every power cycle and nothing is written to flash.
With --flash it is written to the board's flash instead (together with ESP-IDF's standard bootloader and partition table, in
esp32s3/flash/), and the board then starts the receiver by itself at every power-up. That overwrites whatever is in the flash.

Boards with two USB ports have a USB-UART bridge (CH343, CH340, CP210x or FTDI) whose RTS/DTR lines drive EN and BOOT:
esptool resets the chip into its ROM loader through it, no button needed. After loading, the chip's native USB port
shows up with the USB id 303a:1001; that is the port the other tools use.

Needs esptool and pyserial (pip install -r host/python/requirements.txt).
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import time

import espdr_nb as nb

# USB-UART bridges used on ESP32-S3 boards: (vendor, product)
BRIDGES = {(0x1A86, 0x55D3), (0x1A86, 0x7523), (0x1A86, 0x55D4), (0x10C4, 0xEA60), (0x0403, 0x6001), (0x0403, 0x6010),
           (0x0403, 0x6014)}
NATIVE = (0x303A, 0x1001)


def ports():
    from serial.tools import list_ports
    return list(list_ports.comports())


def find_bridge():
    found = [p.device for p in ports() if (p.vid, p.pid) in BRIDGES]
    if len(found) > 1:
        raise SystemExit(f"several USB-UART bridges found ({', '.join(found)}); name the board's with --port")
    return found[0] if found else None


def native_ports():
    return [p.device for p in ports() if (p.vid, p.pid) == NATIVE]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image", nargs="?", default="esp32s3/build-nb/iq-source.bin",
                    help="narrowband image (default: the one `make NARROWBAND=1` builds)")
    ap.add_argument("--port", help="the board's USB-UART bridge port (default: find it)")
    ap.add_argument("--native", action="store_true",
                    help="load through the native USB port; put the chip in download mode first (hold BOOT, tap RESET)")
    ap.add_argument("--flash", action="store_true",
                    help="write the image, ESP-IDF's bootloader and a partition table to the board's flash (overwrites it)")
    ap.add_argument("--yes", action="store_true", help="with --flash: do not ask for confirmation")
    ap.add_argument("--no-verify", action="store_true", help="do not talk to the firmware afterwards")
    args = ap.parse_args()

    if not os.path.exists(args.image):
        raise SystemExit(f"{args.image}: no such file (download iq-source-nb.bin from the GitHub release, or build it)")
    if args.native:
        found = native_ports()
        if len(found) != 1:
            raise SystemExit("expected exactly one ESP32-S3 on its native USB port (303a:1001); "
                             "hold BOOT, tap RESET, and try again")
        port, before = found[0], "usb-reset"
    else:
        port = args.port or find_bridge()
        if not port:
            raise SystemExit("no USB-UART bridge found. Is the board's UART/COM port connected? With only the native "
                             "port, use --native.")
        before = "default-reset"
    if args.flash:
        flash_dir = Path(__file__).resolve().parents[2] / "esp32s3" / "flash"
        parts = [flash_dir / "bootloader.bin", flash_dir / "partition-table.bin"]
        if not all(p.exists() for p in parts):
            raise SystemExit(f"{flash_dir}: bootloader.bin and partition-table.bin are missing (run this from a full clone)")
        print("This writes the bootloader (0x0), a partition table (0x8000) and the application (0x10000) to the board's flash.\n"
              "Whatever program is in the flash now is lost. To get back to RAM-only use, run: python -m esptool erase-flash")
        if not args.yes:
            try:
                answer = input("Continue? [y/N] ")
            except EOFError:
                answer = ""
            if answer.strip().lower() != "y":
                raise SystemExit("not written")
        print(f"writing {args.image} to flash through {port} ...", flush=True)
        cmd = [sys.executable, "-m", "esptool", "--chip", "esp32s3", "--port", port, "--baud", "921600", "--before", before,
               "--after", "hard-reset", "write-flash", "--flash-mode", "dio", "--flash-freq", "80m", "--flash-size", "detect",
               "0x0", str(parts[0]), "0x8000", str(parts[1]), "0x10000", args.image]
    else:
        print(f"loading {args.image} through {port} ...", flush=True)
        cmd = [sys.executable, "-m", "esptool", "--chip", "esp32s3", "--port", port, "--before", before,
               "--after", "no-reset", "--no-stub", "load-ram", args.image]
    if subprocess.call(cmd) != 0:
        raise SystemExit("esptool failed (see above). Check the cable, the port, and that no other program holds it.")

    deadline = time.monotonic() + 8
    found = []
    while time.monotonic() < deadline and not found:
        time.sleep(0.3)
        found = native_ports()
    if not found:
        raise SystemExit("the image was loaded, but no native USB port (303a:1001) appeared. Connect the board's native "
                         "USB port too, not only the UART port.")
    native = found[0]
    if not args.no_verify:
        try:
            link = nb.open_link(native)
            nb.check_firmware(link)
        except nb.ProtocolError as e:
            raise SystemExit(f"the image was loaded, but the firmware did not answer on {native}: {e}")
    print(f"ok: the receiver is on {native}")
    print(f"  try:  python host/python/espdr_nb.py -p {native} bench --seconds 3")
    print(f"        python host/python/espdr_rtltcp.py -p {native}      # for SDR++")
    return 0


if __name__ == "__main__":
    sys.exit(main())
