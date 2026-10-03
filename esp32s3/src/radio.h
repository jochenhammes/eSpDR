/* Receiver: PHY calibration, LO tuning, the ADC IQ path and its settings. */
#pragma once

#include <stdint.h>

/* Settings at boot. */
#define RADIO_LO_HZ 2440000000u /* LO frequency; the stream covers LO +-40 MHz */
#define RADIO_GAIN 24           /* receive gain selector (not dB) */

/*
 * Calibrates the PHY, tunes the RF PLL and routes baseband IQ to the sample
 * dump engine with the boot settings: 80 Msps, 40 MHz analog width, RC
 * filter code 0 and both gain stages from the gain selector. Returns one of
 * ESP_RADIO_* from control.h.
 */
unsigned radio_init(void);

/*
 * Changes one setting (op ESP_SET_*, value in its control.h encoding) with
 * the dump engine stopped, and reconfigures the receive path. Returns a
 * CTL_* status; *effective is the value now in effect.
 */
unsigned radio_set(unsigned op, uint32_t value, uint32_t *effective);

/* Settings in effect, indexed by ESP_STAT_LO_HZ..ESP_STAT_SDM_WORD; 0 otherwise. */
uint32_t radio_stat(unsigned index);

#ifdef ESPDR_TXTEST
/*
 * RESEARCH ONLY (docs/TX-RESEARCH.md): transmits an unmodulated carrier at lo_khz for `ms` milliseconds with the PHY's
 * test-tone setting `g` (larger = weaker; the frontend's gain field is -g), then restores the receiver. Limited to
 * TX_MIN_KHZ..TX_MAX_KHZ, g >= TX_MIN_G and ms <= 5000. *info receives the PLL's sigma-delta word as read back with the
 * carrier on. Returns a CTL_* status.
 */
#define TX_MIN_KHZ 2320000u
#define TX_MAX_KHZ 2400000u
#define TX_MIN_G 64u
unsigned radio_tx_test(uint32_t lo_khz, unsigned g, unsigned ms, uint32_t *info);
#endif

/* The dump engine's control word for the selected sample rate (not running),
 * and the pairs it writes per 16 MHz system timer tick. */
uint32_t radio_dump_control(void);
unsigned radio_pairs_per_tick(void);
