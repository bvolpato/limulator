/*
 * SPDX-License-Identifier: GPL-2.0-or-later
 * Bare-metal AArch64 self-modifying copy and remote jump-cache validation.
 * Boot/UART support derives from YahiaAngelo/qemu-vphone's vphone-fence
 * fixture at commit 38c32a9bf16d92c0fd1d82b20c051cbe34a90372.
 */
typedef unsigned long u64;
typedef unsigned int u32;
#ifndef BLOCKS
#define BLOCKS 512
#endif
#define WORDS 16
#define ROUNDS 2
#define BYTES (BLOCKS * WORDS * sizeof(u32))
static u32 hot[BLOCKS][WORDS] __attribute__((aligned(4096)));
static u32 cold[BLOCKS][WORDS] __attribute__((aligned(4096)));
static u32 source[BLOCKS][WORDS] __attribute__((aligned(4096)));
static volatile u64 command, finished, remote_failures;
static u64 sec_stack[2048] __attribute__((aligned(16)));
u64 psci_call(u64, u64, u64, u64);
void sec_entry(void);
void copy64(void *, const void *, u64);
static void barrier(void) { __asm__ volatile("dmb ish" ::: "memory"); }
static u64 ticks(void)
{
    u64 t;
    __asm__ volatile("isb; mrs %0, cntpct_el0" : "=r"(t) :: "memory");
    return t;
}
static void putc_(char c)
{
    volatile u32 *uart = (volatile u32 *)0x09000000UL;
    while (uart[0x18 / 4] & (1 << 5)) { }
    uart[0] = (u32)c;
}
static void puts_(const char *s)
{
    while (*s) {
        if (*s == '\n') { putc_('\r'); }
        putc_(*s++);
    }
}
static void putd(u64 v)
{
    char b[24];
    int n = 0;
    do { b[n++] = '0' + v % 10; v /= 10; } while (v);
    while (n) { putc_(b[--n]); }
}
void fatal_exception(u64 vec, u64 esr, u64 elr, u64 far)
{
    puts_("FATAL exception vec="); putd(vec);
    puts_(" esr="); putd(esr); puts_(" elr="); putd(elr);
    puts_(" far="); putd(far); puts_("\nTOTAL fail\n");
}
static void sync_code(void *p)
{
    for (u64 a = (u64)p; a < (u64)p + BYTES; a += 64) {
        __asm__ volatile("dc cvau, %0" :: "r"(a) : "memory");
    }
    __asm__ volatile("dsb ish" ::: "memory");
    for (u64 a = (u64)p; a < (u64)p + BYTES; a += 64) {
        __asm__ volatile("ic ivau, %0" :: "r"(a) : "memory");
    }
    __asm__ volatile("dsb ish; isb" ::: "memory");
}
static void make_source(u64 phase)
{
    for (u64 b = 0; b < BLOCKS; b++) {
        source[b][0] = 0xd2800000U | ((b + 1 + phase * 128) << 5);
        for (u64 i = 1; i < WORDS - 1; i++) {
            source[b][i] = 0x91000000U | ((i + phase * 16) << 10);
        }
        source[b][WORDS - 1] = 0xd65f03c0U;
    }
}
static u64 expected(u64 b, u64 phase)
{
    return b + 1 + phase * 128 + 105 + phase * 16 * 14;
}
static u64 run_bank(u32 bank[BLOCKS][WORDS], u64 phase)
{
    u64 errors = 0;
    for (u64 b = 0; b < BLOCKS; b++) {
        u64 (*fn)(void) = (u64 (*)(void))bank[b];
        if (fn() != expected(b, phase)) { errors++; }
    }
    return errors;
}
static u64 check_bytes(u32 bank[BLOCKS][WORDS])
{
    const volatile unsigned char *a = (const volatile unsigned char *)bank;
    const volatile unsigned char *b = (const volatile unsigned char *)source;
    u64 errors = 0;
    for (u64 i = 0; i < BYTES; i++) { errors += a[i] != b[i]; }
    return errors;
}
void secondary_main(void)
{
    u64 seen = 0;
    for (;;) {
        u64 n;
        while ((n = command) == seen) { }
        barrier();
        seen = n;
        __asm__ volatile("isb" ::: "memory");
        remote_failures = run_bank(hot, n - 1);
        barrier();
        finished = n;
    }
}
static u64 run_remote(u64 phase)
{
    barrier();
    command = phase + 1;
    while (finished != phase + 1) { }
    barrier();
    return remote_failures;
}
static void result(const char *name, u64 value)
{
    puts_(name); putd(value); puts_("\n");
}
int main(void)
{
    u64 errors = 0, freq;
    __asm__ volatile("mrs %0, cntfrq_el0" : "=r"(freq));
    puts_("smc-copy: 64-byte functions, 15 changed instructions each\n");
    result("blocks=", BLOCKS); result("bytes=", BYTES); result("counter_hz=", freq);
    if (psci_call(0xC4000003UL, 1, (u64)sec_entry,
                  (u64)(sec_stack + 2048)) != 0) {
        puts_("FAIL CPU_ON\nTOTAL fail\n"); return 1;
    }
    make_source(0);
    copy64(hot, source, BYTES); sync_code(hot);
    errors += run_bank(hot, 0);
    errors += run_remote(0);
    puts_("primed on both CPUs\n");
    u64 stale_control = run_bank(hot, 1);
    result("stale_code_negative_control_mismatches=", stale_control);
    errors += stale_control != BLOCKS;
    for (u64 phase = 1; phase <= ROUNDS; phase++) {
        make_source(phase);
        u64 t = ticks();
        copy64(cold, source, BYTES);
        u64 cold_ticks = ticks() - t;
        errors += check_bytes(cold);
        t = ticks();
        copy64(hot, source, BYTES);
        u64 hot_ticks = ticks() - t;
        sync_code(hot);
        errors += check_bytes(hot);
        errors += run_bank(hot, phase);
        errors += run_remote(phase);
        result("phase=", phase);
        result("cold_copy_ticks=", cold_ticks);
        result("hot_copy_ticks=", hot_ticks);
        result("cumulative_errors=", errors);
    }
    sync_code(cold);
    errors += run_bank(cold, ROUNDS);
    result("verified_function_executions=", (1 + ROUNDS) * BLOCKS * 2 + BLOCKS * 2);
    result("verified_modified_instruction_executions=", ROUNDS * BLOCKS * 2 * 15);
    result("errors=", errors);
    puts_(errors ? "TOTAL fail\n" : "TOTAL ok\n");
    return errors != 0;
}
