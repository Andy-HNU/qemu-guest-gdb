# RISC-V GDB Exception Redirect Demo

This directory contains two tiny raw firmware demos for the RISC-V `virt`
machine. They are intentionally generated from fixed instruction bytes so no
RISC-V cross toolchain is required.

## Firmware

Generate both binaries:

```sh
tests/qemu-gdb/demo/make-demo-firmware.sh
```

The default output directory is `/tmp/qemu-riscv-gdb-demo`.

`store-fault.bin` contains:

```asm
auipc t0, 0
addi  t0, t0, 16
csrw  mtvec, t0
sd    zero, 0(zero)
jal   zero, 0
```

The store to address `0x0` raises the normal RISC-V store access fault.
`mtvec` points at the final self-loop, so after the guest handles the trap the
hart stays idle.

`null-load.bin` contains:

```asm
auipc t0, 0
addi  t0, t0, 16
csrw  mtvec, t0
ld    t0, 0(zero)
jal   zero, 0
```

Both demos work with plain:

```text
-cpu rv64
```

The default `x-gdb-exception-report-mask` now forwards all synchronous guest
exceptions to the gdbstub when GDB is attached.

## Manual GDB Flow

GDB only needs normal remote operations:

```gdb
set architecture riscv:rv64
target remote :1234
c
c
c
c
```

Each `continue` should release one reported hart. With `-smp 4`, GDB should
observe four `SIGTRAP` stops across four different threads. No register write
or PC adjustment is required on the GDB side.
