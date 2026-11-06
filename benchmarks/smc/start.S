// SPDX-License-Identifier: GPL-2.0-or-later
// Boot support from YahiaAngelo/qemu-vphone's tests/tcg/vphone-fence/start.S,
// commit 38c32a9bf16d92c0fd1d82b20c051cbe34a90372. copy64 is the SMC fixture.
// CPU 0 runs main(); CPU 1 is started by main() through PSCI CPU_ON at sec_entry
// with x0 = its stack top, and runs secondary_main().
    .section .text.boot, "ax"
    .global _start
_start:
    mrs     x0, CurrentEL
    lsr     x0, x0, #2
    cmp     x0, #2
    b.ne    el1
    mov     x0, #(1 << 31)
    msr     hcr_el2, x0
    mov     x0, #0x3c5
    msr     spsr_el2, x0
    adr     x0, el1
    msr     elr_el2, x0
    eret
el1:
    ldr     x0, =__stack_top
    mov     sp, x0
    ldr     x0, =vectors
    msr     vbar_el1, x0
    mov     x0, #(3 << 20)
    msr     cpacr_el1, x0
    isb
    ldr     x0, =__bss_start
    ldr     x1, =__bss_end
1:  cmp     x0, x1
    b.hs    2f
    str     xzr, [x0], #8
    b       1b
2:  bl      main
    ldr     x0, =0x84000008
    hvc     #0
park:
    wfe
    b       park

    .global sec_entry
sec_entry:
    mov     sp, x0
    ldr     x0, =vectors
    msr     vbar_el1, x0
    mov     x0, #(3 << 20)
    msr     cpacr_el1, x0
    isb
    bl      secondary_main
    b       park

// u64 psci_call(u64 fn, u64 a, u64 b, u64 c)
    .global psci_call
psci_call:
    hvc     #0
    ret

    .balign 2048
vectors:
    .irp n, 0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15
    .balign 128
    stp     x0, x1, [sp, #-16]!
    mov     x0, #\n
    b       vec_common
    .endr
vec_common:
    mrs     x1, esr_el1
    mrs     x2, elr_el1
    mrs     x3, far_el1
    bl      fatal_exception
    ldr     x0, =0x84000008
    hvc     #0
3:  wfe
    b       3b


    .text
    .global copy64
copy64:
1:  ldp x3, x4, [x1]
    ldp x5, x6, [x1, #16]
    ldp x7, x8, [x1, #32]
    ldp x9, x10, [x1, #48]
    stp x3, x4, [x0]
    stp x5, x6, [x0, #16]
    stp x7, x8, [x0, #32]
    stp x9, x10, [x0, #48]
    add x0, x0, #64
    add x1, x1, #64
    subs x2, x2, #64
    b.ne 1b
    ret
