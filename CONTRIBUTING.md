# Contributing

Thanks for looking. Two kinds of contribution help most:

1. **Hardware reports.** The narrowband mode has been tested on one board and one host. Open an issue with the
   *Board / hardware report* template, whether it worked or not.
2. **Bug reports and fixes** for the Python tools, the bridge and the narrowband firmware.

Before sending a pull request, run what CI runs:

```sh
pip install -r host/python/requirements-dev.txt
python esp32s3/tools/dsp_selftest.py
python host/python/espdr_nb.py selftest
python host/python/espdr_rtltcp.py selftest
```

If you touch the firmware, also build it (`make -C esp32s3 NARROWBAND=1` and plain `make -C esp32s3`; the second must stay
byte-identical to upstream's) and, if you have a board, run `python host/python/espdr_nb.py dspbench`: it must report
0 differing bytes and stay below 100 % of the cycle budget. Rules that keep the firmware working are in
[docs/NARROWBAND-INTERNALS.md](docs/NARROWBAND-INTERNALS.md) (no ROM calls, no division, one code copy per core).

Code and documentation are in English. If a large part of a change was written with an AI assistant, say so in the pull request, as the
project does itself.
