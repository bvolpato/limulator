// SPDX-License-Identifier: GPL-2.0-or-later
    .text
    .global fixed_loop_ticks
fixed_loop_ticks:
    mrs x1, cntvct_el0
1:  subs x0, x0, #1
    b.ne 1b
    mrs x2, cntvct_el0
    sub x0, x2, x1
    ret
    .global wait_until
wait_until:
    wfit x0
    mrs x0, cntvct_el0
    ret
