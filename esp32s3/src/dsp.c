/*
 * Narrowband decimation: 16 Msps IQ -> CIC4 /16 -> FIR /R2.
 *
 * Output sample k is a function of input pairs only (never of earlier
 * output), so each capture unit can be computed independently, by either
 * core: the unit re-reads the end of the unit before it as filter history.
 * Every output belongs to exactly one unit: the one holding the last input
 * pair that influences it.
 *
 *   stage 1 output j  = CIC over input block j (pairs 16j .. 16j+15)
 *   stage 2 output k  = FIR over stage-1 outputs R2*k-(T-1) .. R2*k
 *
 * The CIC integrators start from zero NCIC blocks before the first block the
 * FIR needs, which reproduces the true response exactly from there on.
 * Only 32-bit arithmetic is used: core 1 must never call into the ROM
 * (64-bit helpers would), because captures overwrite the ROM's data.
 */
#include "dsp.h"

#include "dsp_taps.h"
#ifdef PROFILE
extern volatile uint32_t dsp_prof[4];
static inline uint32_t cc(void){uint32_t c; __asm__ volatile("rsr.ccount %0":"=a"(c)); return c;}
#define PT(i,e) (dsp_prof[i] += (e))
#else
#define cc() 0
#define PT(i,e) ((void)(e))
#endif

#define R1 DSP_R1
#define NCIC 4u
#define RING_MASK (DSP_RING_PAIRS - 1u)
#define HIST 128u    /* stage-1 history ring, power of two >= longest FIR */
#define CIC_SHIFT 11 /* CIC gain 2^16, 10-bit input: full scale -> +-2^14 */
#define IDLE_BLOCKS 32u

#ifndef DSP_FUNC
#define DSP_FUNC dsp_unit
#define DSP_ATTR
#endif

/* I is bits 0..9 and Q bits 10..19 of a capture word, two's complement. */
static inline int32_t sx_i(uint32_t w) { return (int32_t)(w << 22) >> 22; }
static inline int32_t sx_q(uint32_t w) { return (int32_t)(w << 12) >> 22; }

static inline int32_t clamp(int32_t v, int32_t lo, int32_t hi) { return v < lo ? lo : v > hi ? hi : v; }

/*
 * Stage-1 integrators over one block of R1 pairs. A function of its own so the eight accumulators
 * stay in registers: inlined into dsp_unit() they were spilled to the stack, which cost about 3x.
 */
static void DSP_ATTR __attribute__((noinline)) cic_block(const uint32_t *p, uint32_t *ai, uint32_t *aq)
{
    uint32_t a1 = ai[0], a2 = ai[1], a3 = ai[2], a4 = ai[3];
    uint32_t b1 = aq[0], b2 = aq[1], b3 = aq[2], b4 = aq[3];
    for (unsigned n = 0; n < R1; n++) {
        uint32_t w = p[n];
        a1 += (uint32_t)sx_i(w);
        a2 += a1;
        a3 += a2;
        a4 += a3;
        b1 += (uint32_t)sx_q(w);
        b2 += b1;
        b3 += b2;
        b4 += b3;
    }
    ai[0] = a1; ai[1] = a2; ai[2] = a3; ai[3] = a4;
    aq[0] = b1; aq[1] = b2; aq[2] = b3; aq[3] = b4;
}

/*
 * Symmetric FIR over both channels at once: the taps are symmetric (odd length), so the two samples that
 * share a tap are added first. Own function so the pointers and accumulators stay in registers.
 */
static void DSP_ATTR __attribute__((noinline)) fir_sym(const int16_t *h, const int16_t *wi, const int16_t *wq,
                                                       unsigned taps, int32_t *out)
{
    const unsigned mid = taps / 2;
    const int16_t *ia = wi, *ib = wi + taps - 1, *qa = wq, *qb = wq + taps - 1;
    int32_t accI = (int32_t)h[mid] * wi[mid], accQ = (int32_t)h[mid] * wq[mid];
    for (unsigned n = 0; n < mid; n++) {
        const int32_t c = h[n];
        accI += c * ((int32_t)*ia++ + *ib--);
        accQ += c * ((int32_t)*qa++ + *qb--);
    }
    out[0] = accI;
    out[1] = accQ;
}

#ifdef __XTENSA__
/* Set by the on-chip test (NB_DSPBENCH) to compare the SIMD path with the plain C one. */
extern volatile uint32_t dsp_scalar_only;

/*
 * One FIR output from the ESP32-S3's SIMD unit: 8 multiply-accumulates per instruction into ACCX.
 * `w` is the 16-byte aligned start of the vectors covering the window, `t` the tap row shifted to match
 * (dsp_taps_pie_*), nvec vectors each. Taps outside the window are zero, so the extra samples are harmless.
 */
static int32_t DSP_ATTR __attribute__((noinline)) fir_pie(const int16_t *w, const int16_t *t, unsigned nvec)
{
    int32_t r;
    __asm__ volatile("ee.zero.accx\n"
                     "loopnez %3, 1f\n"
                     "ee.vld.128.ip q0, %0, 16\n"
                     "ee.vld.128.ip q1, %1, 16\n"
                     "ee.vmulas.s16.accx q0, q1\n"
                     "1:\n"
                     "rur.accx_0 %2\n"
                     : "+a"(w), "+a"(t), "=a"(r)
                     : "a"(nvec)
                     : "memory");
    return r;
}
#endif

unsigned DSP_ATTR DSP_FUNC(const dsp_config *cfg, const uint32_t *cur, const uint32_t *prev, unsigned first,
                           unsigned count, uint32_t start, int have_prev, uint8_t *out, void (*idle)(void))
{
    const unsigned r2 = cfg->r2;
    const unsigned taps = r2 == 2 ? DSP_TAPS_R2_2 : DSP_TAPS_R2_4;
    const int16_t *h = r2 == 2 ? dsp_taps_r2_2 : dsp_taps_r2_4;
#ifdef __XTENSA__
    const int16_t *pie_taps = r2 == 2 ? &dsp_taps_pie_r2_2[0][0] : &dsp_taps_pie_r2_4[0][0];
    const unsigned pie_vecs = r2 == 2 ? DSP_PIE_VECS_R2_2 : DSP_PIE_VECS_R2_4;
#endif
    const dsp_geometry g = dsp_geom(start, count, r2);

    /* Blocks are numbered relative to the unit's first complete block (rel 0). */
    int32_t rel = (int32_t)g.skip - (int32_t)(taps - 1u) - (int32_t)NCIC;
    if (!have_prev && rel < 0)
        rel = 0;
    const int32_t rel_end = (int32_t)g.nb;

    uint32_t ai[NCIC] = {0}, aq[NCIC] = {0}; /* integrators */
    uint32_t di[NCIC] = {0}, dq[NCIC] = {0}; /* comb delays */
    /* Static, not on the stack: at stack offsets beyond 510 bytes every access costs three instructions.
     * Each build of this file (core 0's and core 1's copy) has its own, and each runs on one core only. */
    static int16_t hi[2 * HIST] __attribute__((aligned(16))), hq[2 * HIST] __attribute__((aligned(16)));
    for (unsigned n = 0; n < 2 * HIST; n++)
        hi[n] = hq[n] = 0;
    unsigned pos = 0, produced = 0, tick = 0;
    uint32_t tmp[R1];
    const unsigned shift = cfg->shift;

    uint32_t cs = cc(); (void)cs;
    for (; rel < rel_end; rel++) {
        /* The block's first pair, relative to the unit's first pair. */
        const int32_t off = g.d - (int32_t)(R1 - 1u) + rel * (int32_t)R1;
        const uint32_t *p;
        if (off >= 0) {
            unsigned idx = (first + (unsigned)off) & RING_MASK;
            if (idx + R1 <= DSP_RING_PAIRS) {
                p = cur + idx;
                goto have_block;
            }
        } else if (off + (int32_t)R1 <= 0) {
            unsigned idx = (first + (unsigned)off) & RING_MASK;
            if (idx + R1 <= DSP_RING_PAIRS) {
                p = prev + idx;
                goto have_block;
            }
        }
        for (unsigned n = 0; n < R1; n++) {
            int32_t o = off + (int32_t)n;
            unsigned idx = (first + (unsigned)o) & RING_MASK;
            tmp[n] = o >= 0 ? cur[idx] : prev[idx];
        }
        p = tmp;
    have_block:;

        /* Stage 1: integrate 16 pairs, then comb. */
        uint32_t c0 = cc();
        cic_block(p, ai, aq);
        uint32_t c1 = cc(); PT(0, c1 - c0);
        uint32_t a4 = ai[3], b4 = aq[3];

        uint32_t vi = a4, vq = b4, t;
        for (unsigned c = 0; c < NCIC; c++) {
            t = vi - di[c]; di[c] = vi; vi = t;
            t = vq - dq[c]; dq[c] = vq; vq = t;
        }
        hi[pos] = hi[pos + HIST] = (int16_t)((int32_t)vi >> CIC_SHIFT);
        hq[pos] = hq[pos + HIST] = (int16_t)((int32_t)vq >> CIC_SHIFT);
        pos = (pos + 1) & (HIST - 1);

        uint32_t c2 = cc(); PT(1, c2 - c1);
        if (idle && ++tick == IDLE_BLOCKS) {
            tick = 0;
            idle();
        }

        /* Stage 2: every R2-th stage-1 output from the first trigger on. */
        if (rel < (int32_t)g.skip || ((uint32_t)rel - g.skip) & (r2 - 1u))
            continue;
        const int16_t *wi = hi + ((pos - taps) & (HIST - 1));
        const int16_t *wq = hq + ((pos - taps) & (HIST - 1));
        uint32_t c3 = cc();
        int32_t acc[2];
#ifdef __XTENSA__
        if (!dsp_scalar_only) {
            const unsigned s = (pos - taps) & (HIST - 1), a = s & 7;
            acc[0] = fir_pie(hi + (s - a), pie_taps + a * (pie_vecs * 8), pie_vecs);
            acc[1] = fir_pie(hq + (s - a), pie_taps + a * (pie_vecs * 8), pie_vecs);
        } else
#endif
        fir_sym(h, wi, wq, taps, acc);
        const int32_t accI = acc[0], accQ = acc[1];
        int32_t yi = (accI + (1 << 14)) >> 15;
        int32_t yq = (accQ + (1 << 14)) >> 15;
        if (shift) {
            yi = (yi + (1 << (shift - 1))) >> shift;
            yq = (yq + (1 << (shift - 1))) >> shift;
        }
        if (cfg->format == 0) {
            yi = clamp(yi, -32768, 32767);
            yq = clamp(yq, -32768, 32767);
            uint8_t *o = out + produced * 4;
            o[0] = (uint8_t)yi;
            o[1] = (uint8_t)(yi >> 8);
            o[2] = (uint8_t)yq;
            o[3] = (uint8_t)(yq >> 8);
        } else {
            uint8_t *o = out + produced * 2;
            o[0] = (uint8_t)(int8_t)clamp(yi, -128, 127);
            o[1] = (uint8_t)(int8_t)clamp(yq, -128, 127);
        }
        produced++;
        PT(2, cc() - c3);
    }
    return produced;
}
