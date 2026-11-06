/* SPDX-License-Identifier: GPL-2.0-or-later
 * From YahiaAngelo/qemu-vphone's tests/tcg/vphone-fence/link.ld,
 * commit 38c32a9bf16d92c0fd1d82b20c051cbe34a90372.
 */
ENTRY(_start)
SECTIONS
{
    . = 0x40080000;
    .text : { KEEP(*(.text.boot)) *(.text .text.*) }
    .rodata : { *(.rodata .rodata.*) }
    .data : { *(.data .data.*) }
    . = ALIGN(4096);
    __bss_start = .;
    .bss (NOLOAD) : { *(.bss .bss.*) *(COMMON) }
    . = ALIGN(16);
    __bss_end = .;
    . = . + 0x10000;
    __stack_top = .;
}
