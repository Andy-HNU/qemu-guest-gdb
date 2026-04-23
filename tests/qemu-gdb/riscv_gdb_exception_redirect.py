#!/usr/bin/env python3
#
# Verify that redirected RISC-V guest exceptions leave cpu_exec through
# EXCP_DEBUG and reach the gdbstub stop-packet path under multi-vCPU TCG.

import argparse
import os
import re
import socket
import subprocess
import tempfile
import time


def checksum(payload):
    return sum(payload.encode("ascii")) & 0xff


def send_packet(sock, payload):
    packet = f"${payload}#{checksum(payload):02x}".encode("ascii")
    sock.sendall(packet)
    ack = sock.recv(1)
    if ack != b"+":
        raise RuntimeError(f"gdbstub rejected packet {payload!r}: ack={ack!r}")


def recv_packet(sock, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        sock.settimeout(max(0.1, end - time.monotonic()))
        ch = sock.recv(1)
        if ch in (b"+", b"-"):
            continue
        if ch != b"$":
            continue

        data = bytearray()
        while True:
            ch = sock.recv(1)
            if ch == b"#":
                break
            data.extend(ch)
        got_sum = sock.recv(2)
        payload = data.decode("ascii")
        want_sum = f"{checksum(payload):02x}".encode("ascii")
        if got_sum.lower() != want_sum:
            sock.sendall(b"-")
            raise RuntimeError(
                f"bad checksum for {payload!r}: got {got_sum!r}, want {want_sum!r}"
            )
        sock.sendall(b"+")
        return payload
    raise TimeoutError("timed out waiting for a gdb remote packet")


def choose_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def connect_gdbstub(port, qemu, timeout):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        if qemu.poll() is not None:
            stderr = ""
            if qemu.stderr:
                stderr = qemu.stderr.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"QEMU exited before gdb connection:\n{stderr}")
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=0.2)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            return sock
        except OSError as err:
            last_error = err
            time.sleep(0.05)
    raise TimeoutError(f"could not connect to gdbstub: {last_error}")


def redirect_count(qemu_log):
    pattern = "cpu_handle_exception: redirect exception"
    try:
        with open(qemu_log, "r", encoding="utf-8", errors="replace") as log:
            return log.read().count(pattern)
    except FileNotFoundError:
        return 0


def stop_thread(packet):
    match = re.search(r"thread:([^;]+);", packet)
    if not match:
        raise AssertionError(f"stop packet has no thread id: {packet!r}")
    return match.group(1)


def scenario_firmware(scenario):
    if scenario == "store-fault":
        return bytes.fromhex(
            "97020000"  # auipc t0, 0
            "93820201"  # addi  t0, t0, 16
            "73905230"  # csrw  mtvec, t0
            "23300000"  # sd    zero, 0(zero)
            "6f000000"  # jal   zero, 0
        )
    if scenario == "null-load":
        return bytes.fromhex(
            "97020000"  # auipc t0, 0
            "93820201"  # addi  t0, t0, 16
            "73905230"  # csrw  mtvec, t0
            "83320000"  # ld t0, 0(zero)
            "6f000000"  # jal zero, 0
        )
    raise ValueError(f"unknown scenario {scenario!r}")


def scenario_cpu(scenario):
    if scenario == "store-fault":
        return "rv64"
    if scenario == "null-load":
        return "rv64"
    raise ValueError(f"unknown scenario {scenario!r}")


def run_test(qemu_path, timeout, smp, qemu_log=None, trace_gdbstub=False,
             qemu_stderr=None, scenario="store-fault"):
    port = choose_port()
    with tempfile.TemporaryDirectory(prefix="qemu-riscv-gdb-exc-") as tmpdir:
        firmware = os.path.join(tmpdir, "loop.bin")
        with open(firmware, "wb") as fw:
            fw.write(scenario_firmware(scenario))

        cmd = [
            qemu_path,
            "-machine", "virt",
            "-accel", "tcg,thread=multi",
            "-cpu", scenario_cpu(scenario),
            "-smp", str(smp),
            "-m", "128M",
            "-nographic",
            "-serial", "none",
            "-monitor", "none",
            "-bios", firmware,
            "-S",
            "-gdb", f"tcp:127.0.0.1:{port},server=on,wait=off",
        ]
        if qemu_log:
            cmd += ["-d", "mmu,int", "-D", qemu_log]
        if trace_gdbstub:
            cmd += [
                "-trace", "gdbstub_op_continue",
                "-trace", "gdbstub_hit_break",
                "-trace", "gdbstub_hit_paused",
                "-trace", "gdbstub_io_reply",
            ]

        stderr_file = open(qemu_stderr, "wb") if qemu_stderr else subprocess.PIPE
        qemu = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=stderr_file)
        try:
            with connect_gdbstub(port, qemu, timeout) as sock:
                send_packet(sock, "?")
                recv_packet(sock, timeout)

                if qemu_log:
                    deadline = time.monotonic() + timeout
                    count = 0
                    stopped_threads = set()
                    while len(stopped_threads) < smp and time.monotonic() < deadline:
                        send_packet(sock, "c")
                        packet = recv_packet(
                            sock, max(0.1, deadline - time.monotonic())
                        )
                        if not (packet.startswith("T05") or packet.startswith("S05")):
                            raise AssertionError(
                                f"expected GDB SIGTRAP stop, got {packet!r}"
                            )
                        stopped_threads.add(stop_thread(packet))
                        count = redirect_count(qemu_log)
                    if len(stopped_threads) < smp:
                        raise AssertionError(
                            f"expected {smp} GDB stop threads, saw "
                            f"{len(stopped_threads)}: {sorted(stopped_threads)}"
                        )
                    if count != smp:
                        raise AssertionError(
                            f"expected {smp} redirected CPU exceptions, saw {count}"
                        )
                else:
                    send_packet(sock, "c")
                    packet = recv_packet(sock, timeout)
                    if not (packet.startswith("T05") or packet.startswith("S05")):
                        raise AssertionError(
                            f"expected GDB SIGTRAP stop, got {packet!r}"
                        )
        finally:
            qemu.terminate()
            try:
                qemu.wait(timeout=2)
            except subprocess.TimeoutExpired:
                qemu.kill()
                qemu.wait(timeout=2)
            if qemu_stderr:
                stderr_file.close()


def run_no_attach_test(qemu_path, timeout, smp, qemu_log, qemu_stderr=None,
                       scenario="store-fault"):
    port = choose_port()
    with tempfile.TemporaryDirectory(prefix="qemu-riscv-gdb-exc-") as tmpdir:
        firmware = os.path.join(tmpdir, "loop.bin")
        with open(firmware, "wb") as fw:
            fw.write(scenario_firmware(scenario))

        cmd = [
            qemu_path,
            "-machine", "virt",
            "-accel", "tcg,thread=multi",
            "-cpu", scenario_cpu(scenario),
            "-smp", str(smp),
            "-m", "128M",
            "-nographic",
            "-serial", "none",
            "-monitor", "none",
            "-bios", firmware,
            "-gdb", f"tcp:127.0.0.1:{port},server=on,wait=off",
            "-d", "mmu,int",
            "-D", qemu_log,
        ]

        stderr_file = open(qemu_stderr, "wb") if qemu_stderr else subprocess.PIPE
        qemu = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=stderr_file)
        try:
            time.sleep(timeout)
            count = redirect_count(qemu_log)
            if count:
                raise AssertionError(
                    f"expected no redirected exceptions without GDB, saw {count}"
                )
        finally:
            qemu.terminate()
            try:
                qemu.wait(timeout=2)
            except subprocess.TimeoutExpired:
                qemu.kill()
                qemu.wait(timeout=2)
            if qemu_stderr:
                stderr_file.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--qemu", required=True)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--smp", type=int, default=4)
    parser.add_argument("--qemu-log")
    parser.add_argument("--trace-gdbstub", action="store_true")
    parser.add_argument("--qemu-stderr")
    parser.add_argument("--no-attach", action="store_true")
    parser.add_argument("--scenario", choices=("store-fault", "null-load"),
                        default="store-fault")
    args = parser.parse_args()
    if args.no_attach:
        if not args.qemu_log:
            parser.error("--no-attach requires --qemu-log")
        run_no_attach_test(args.qemu, args.timeout, args.smp, args.qemu_log,
                           args.qemu_stderr, args.scenario)
    else:
        run_test(args.qemu, args.timeout, args.smp, args.qemu_log,
                 args.trace_gdbstub, args.qemu_stderr, args.scenario)


if __name__ == "__main__":
    main()
