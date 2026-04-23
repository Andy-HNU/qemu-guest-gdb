#!/usr/bin/env bash
set -eu

outdir="${1:-/tmp/qemu-riscv-gdb-demo}"
mkdir -p "$outdir"

# store-fault.bin:
#   0x80000000: auipc t0, 0
#   0x80000004: addi  t0, t0, 16
#   0x80000008: csrw  mtvec, t0
#   0x8000000c: sd    zero, 0(zero)
#   0x80000010: jal   zero, 0
#
# The store to address 0 uses QEMU/RISC-V's existing store access fault path.
# mtvec points at the final self-loop so the guest handles the trap and then
# stays quiescent.
printf '\x97\x02\x00\x00\x93\x82\x02\x01\x73\x90\x52\x30\x23\x30\x00\x00\x6f\x00\x00\x00' > "$outdir/store-fault.bin"

# null-load.bin:
#   0x80000000: auipc t0, 0
#   0x80000004: addi  t0, t0, 16
#   0x80000008: csrw  mtvec, t0
#   0x8000000c: ld    t0, 0(zero)
#   0x80000010: jal   zero, 0
#
# The load from address 0 uses QEMU/RISC-V's existing load access fault path.
# mtvec points at the final self-loop so the guest handles the exception and
# then stays quiescent.
printf '\x97\x02\x00\x00\x93\x82\x02\x01\x73\x90\x52\x30\x83\x32\x00\x00\x6f\x00\x00\x00' > "$outdir/null-load.bin"

printf 'wrote %s/store-fault.bin\n' "$outdir"
printf 'wrote %s/null-load.bin\n' "$outdir"
