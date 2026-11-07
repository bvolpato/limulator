/* SPDX-License-Identifier: GPL-2.0-or-later
 * Diskless timer regression, using vphone-cnt boot/UART/PSCI support.
 * Source: YahiaAngelo/qemu-vphone, tests/tcg/vphone-cnt,
 * commit 108e1421d208e2f17d6a8d5b98ac7659c3517873.
 */
typedef unsigned long u64;
typedef unsigned int u32;
#define UART0 0x09000000UL
static void putc_(char c) {
    volatile u32 *uart = (volatile u32 *)UART0;
    while (uart[0x18 / 4] & (1 << 5)) {}
    uart[0] = (u32)c;
}
static void text(const char *s) { while (*s) { if (*s == '\n') putc_('\r'); putc_(*s++); } }
static void number(u64 n) {
    char buffer[24]; int i = 0;
    do { buffer[i++] = '0' + n % 10; n /= 10; } while (n);
    while (i) putc_(buffer[--i]);
}
static void value(const char *name, u64 n) { text(name); number(n); text("\n"); }
void fatal_exception(u64 vec, u64 esr, u64 elr, u64 far) {
    text("FATAL\n"); value("vector=",vec); value("esr=",esr); value("elr=",elr); value("far=",far);
}
static inline u64 vct(void) { u64 n; __asm__ volatile("mrs %0, cntvct_el0" : "=r"(n)); return n; }
extern u64 fixed_loop_ticks(u64 iterations);
extern u64 wait_until(u64 deadline);
int main(void) {
    u64 frequency, features, el;
    int failures = 0;
    __asm__ volatile("mrs %0, cntfrq_el0" : "=r"(frequency));
    __asm__ volatile("mrs %0, id_aa64isar2_el1" : "=r"(features));
    __asm__ volatile("mrs %0, CurrentEL" : "=r"(el));
    value("counter_hz=", frequency); value("wfxt=", features & 15); value("el=", el >> 2);
    failures += (features & 15) < 2 || (el >> 2) != 1;
#if TEST_MODE == 1
    text("case=rate\n");
    const u64 iterations = 1000000;
    u64 ticks = fixed_loop_ticks(iterations);
    u64 expected = (2 * iterations) * frequency / 1000000000UL;
    u64 error = ticks > expected ? ticks - expected : expected - ticks;
    value("loop_instructions=", 2 * iterations); value("ticks=", ticks);
    value("expected_ticks=", expected); value("tolerance_ticks=", 4);
    failures += error > 4;
#else
    text("case=wfit\n");
    __asm__ volatile("msr daifset, #15; msr cntp_ctl_el0, xzr; msr cntv_ctl_el0, xzr; isb" ::: "memory");
    while (vct() < frequency / 20) {}
    u64 early = 0;
    for (u64 trial = 0; trial < 8; trial++) {
        u64 before = vct();
        u64 deadline = before + frequency / 100;
        u64 after = wait_until(deadline);
        value("trial=", trial); value("before=", before); value("deadline=", deadline); value("after=", after);
        early += after < deadline;
    }
    value("early_returns=", early);
    failures += early != 0;
#endif
    value("failures=", failures);
    text(failures ? "TOTAL fail\n" : "TOTAL ok\n");
    return failures;
}
