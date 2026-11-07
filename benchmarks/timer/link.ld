/* SPDX-License-Identifier: GPL-2.0-or-later
 * Source: YahiaAngelo/qemu-vphone tests/tcg/vphone-cnt/link.ld,
 * commit 108e1421d208e2f17d6a8d5b98ac7659c3517873.
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
