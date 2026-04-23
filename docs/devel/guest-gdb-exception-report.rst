Guest Exception Reporting Through GDB
=====================================

背景
----

这个改动的目标是让 guest 内部已经产生的同步异常能够先通过
QEMU gdbstub 上报给远端 GDB，然后在用户继续执行后再交还给 guest
原有异常处理流程。

设计约束是：异常产生点不携带额外的 GDB 状态。MMU fault、访存
fault、非法指令等异常仍然使用 QEMU 原有路径设置
``CPUState::exception_index`` 和架构相关 trap 信息。GDB 上报只在
TCG 的统一异常处理点做一次拦截。

这避免了在每一种 guest 异常产生点做霰弹式修改。后续要支持新的
异常类型时，只需要扩展架构 hook 的判断逻辑或调整 report mask。

总体流程
--------

原有异常路径仍然负责“产生异常”：

.. code-block:: text

   guest instruction
        |
        v
   TCG helper / MMU / memory access
        |
        v
   target code raises guest exception
        |
        v
   cpu->exception_index = target exception
        |
        v
   cpu_loop_exit_restore()

新增逻辑只在 ``cpu_handle_exception()`` 中判断“是否先上报 GDB”：

.. code-block:: text

   cpu_exec()
        |
        v
   cpu_handle_exception(cpu, &ret)
        |
        +-- non-target exit request
        |       |
        |       v
        |   keep original QEMU flow
        |
        +-- target synchronous exception
                |
                v
        cpu_gdb_exception_report(cpu)
                |
                +-- NONE
                |     |
                |     v
                |  do_interrupt(), guest handles exception
                |
                +-- REPORT
                |     |
                |     v
                |  ret = EXCP_DEBUG
                |  keep cpu->exception_index unchanged
                |  return to cpu_exec outer loop
                |     |
                |     v
                |  existing gdbstub stop-packet path
                |
                +-- DELIVER
                |     |
                |     v
                |  do_interrupt(), guest handles exception
                |  reopen gate
                |
                +-- WAIT
                      |
                      v
                   ret = EXCP_INTERRUPT
                   keep cpu->exception_index unchanged
                   wait for a later continue round

核心拦截点
----------

通用拦截点在 ``accel/tcg/cpu-exec.c`` 的
``cpu_handle_exception()``。它只处理已经成型的 target exception，
不改变异常产生路径。

关键逻辑如下：

.. code-block:: c

   if (cpu->exception_index < EXCP_INTERRUPT) {
       gdb_exception_report = cpu_gdb_exception_report(cpu);
   }

   if (unlikely(gdb_exception_report == GDB_EXCEPTION_REPORT)) {
       qemu_log_mask(CPU_LOG_INT,
                     "%s: redirect exception %d to gdb debug on CPU %d\n",
                     __func__, cpu->exception_index, cpu->cpu_index);
       *ret = EXCP_DEBUG;
       cpu_handle_debug_exception(cpu);
       return true;
   }

   if (unlikely(gdb_exception_report == GDB_EXCEPTION_WAIT)) {
       *ret = EXCP_INTERRUPT;
       return true;
   }

当返回 ``GDB_EXCEPTION_REPORT`` 时，``cpu_handle_exception()`` 不清除
``cpu->exception_index``，而是把本次退出改成 ``EXCP_DEBUG``。这样
外层仍然走 QEMU 原有的 debug stop 和 gdbstub ``put_packet`` 链路。

当用户在 GDB 里执行 ``continue`` 后，同一个 CPU 会再次进入
``cpu_handle_exception()``。此时 gate owner 是自己，返回
``GDB_EXCEPTION_DELIVER``，于是执行原有 ``do_interrupt()``，真正把
异常交付给 guest。

架构 hook
---------

通用 TCG 层不理解各架构异常编号，因此在 ``TCGCPUOps`` 中新增
``gdb_exception_report`` hook：

.. code-block:: c

   typedef enum GDBExceptionReport {
       GDB_EXCEPTION_NONE,
       GDB_EXCEPTION_REPORT,
       GDB_EXCEPTION_DELIVER,
       GDB_EXCEPTION_WAIT,
   } GDBExceptionReport;

   struct TCGCPUOps {
       bool (*gdb_exception_report)(CPUState *cpu);
       ...
   };

RISC-V 在 ``target/riscv/cpu.c`` 注册该 hook：

.. code-block:: c

   static const struct TCGCPUOps riscv_tcg_ops = {
       ...
       .gdb_exception_report = riscv_cpu_gdb_exception_report,
       .do_interrupt = riscv_cpu_do_interrupt,
       ...
   };

RISC-V 的判断函数位于 ``target/riscv/cpu_helper.c``。它只基于当前
已经存在的 ``exception_index`` 和配置 mask 判断是否需要上报：

.. code-block:: c

   bool riscv_cpu_gdb_exception_report(CPUState *cs)
   {
   #ifndef CONFIG_USER_ONLY
       RISCVCPU *cpu = RISCV_CPU(cs);
       uint32_t exception;

       if (!gdb_is_attached() || cs->exception_index < 0 ||
           (cs->exception_index & RISCV_EXCP_INT_FLAG)) {
           return false;
       }

       exception = cs->exception_index & RISCV_EXCP_INT_MASK;
       if (exception < 64 &&
           (cpu->cfg.gdb_exception_report_mask & (1ULL << exception))) {
           return true;
       }

       return false;
   #else
       return false;
   #endif
   }

这里有两个关键点：

* ``gdb_is_attached()`` 为 false 时不启动新流程。没有 GDB 连接时，
  guest 异常直接走 QEMU 原有处理。
* 只处理同步异常。带 ``RISCV_EXCP_INT_FLAG`` 的中断不会被重定向到
  GDB。

RISC-V 暴露了一个调试配置项：

.. code-block:: c

   DEFINE_PROP_UINT64("x-gdb-exception-report-mask", RISCVCPU,
                      cfg.gdb_exception_report_mask, UINT64_MAX),

默认值是 ``UINT64_MAX``，表示默认上报所有 64 以内的 RISC-V 同步
异常。用户如果需要缩小范围，可以在 ``-cpu`` 上指定 mask。例如只
上报 ``RISCV_EXCP_LOAD_ACCESS_FAULT`` 可以使用 ``0x20``。

GDB 连接判断
------------

``gdb_is_attached()`` 位于 ``gdbstub/gdbstub.c``，并通过
``include/exec/gdbstub.h`` 暴露给其他模块。

.. code-block:: c

   bool gdb_is_attached(void)
   {
   #ifdef CONFIG_USER_ONLY
       return gdbserver_state.init && gdbserver_state.fd >= 0;
   #else
       return gdbserver_state.init && gdbserver_state.connected;
   #endif
   }

system mode 下，``connected`` 在 gdbstub chardev 事件中维护：

.. code-block:: c

   case CHR_EVENT_OPENED:
       s->connected = true;
       ...
       vm_stop(RUN_STATE_PAUSED);
       break;

   case CHR_EVENT_CLOSED:
       s->connected = false;
       s->c_cpu = NULL;
       s->g_cpu = NULL;
       break;

这样可以保证本功能只在远端 GDB 已连接时生效。普通无 GDB 的启动
路径不会因为新增逻辑停进 debug stop。

多 vCPU 并发
------------

MTTCG 下多个 vCPU 可能同时命中可上报异常。如果它们都直接转成
``EXCP_DEBUG``，一次 VM stop 可能合并多个 CPU 状态；GDB 侧也可能
只按当前 stop reason 观察到其中一个 CPU。更严重的是，在 VM stop
期间如果某个 CPU 因新逻辑进入不合适的休眠/等待状态，主线程
``pause_all_vcpus()`` 等待 CPU 退出时可能形成死循环。

因此通用 TCG 层维护一个全局 gate：

.. code-block:: c

   #define GDB_EXCEPTION_GATE_OPEN (-1)

   static int gdb_exception_gate = GDB_EXCEPTION_GATE_OPEN;

gate 的状态只有两类：

* ``GDB_EXCEPTION_GATE_OPEN``：当前没有 CPU 正在占用上报权。
* ``cpu_index``：某个 CPU 已经占用上报权，正在完成“上报 GDB ->
  GDB continue -> 交付 guest”的两阶段流程。

竞争逻辑在 ``cpu_gdb_exception_report()``：

.. code-block:: c

   owner = qatomic_read(&gdb_exception_gate);
   if (owner == cpu->cpu_index) {
       return GDB_EXCEPTION_DELIVER;
   }
   if (owner != GDB_EXCEPTION_GATE_OPEN) {
       return GDB_EXCEPTION_WAIT;
   }
   if (qatomic_cmpxchg(&gdb_exception_gate, GDB_EXCEPTION_GATE_OPEN,
                       cpu->cpu_index) == GDB_EXCEPTION_GATE_OPEN) {
       return GDB_EXCEPTION_REPORT;
   }
   return GDB_EXCEPTION_WAIT;

多 vCPU 时序如下：

.. code-block:: text

   CPU0                         CPU1                         CPU2/CPU3
    |                            |                            |
    | target exception           | target exception           | target exception
    v                            v                            v
   gate OPEN
    |
    | cmpxchg OPEN -> 0
    v
   REPORT                       sees owner 0                 sees owner 0
    |                            |                            |
    | ret = EXCP_DEBUG           WAIT                         WAIT
    |                            |                            |
    | GDB stop packet            ret = EXCP_INTERRUPT         ret = EXCP_INTERRUPT
    |                            keep exception pending       keep exception pending
    v
   GDB continue
    |
    v
   owner == CPU0
    |
    v
   DELIVER
    |
    | do_interrupt()
    | cmpxchg owner 0 -> OPEN
    v
   gate OPEN
                                 |
                                 | next scheduling round
                                 v
                                one waiting CPU claims gate

``WAIT`` 的 CPU 不清除 ``exception_index``，也不把异常交付给 guest。
它只是用 ``EXCP_INTERRUPT`` 退出当前 ``cpu_exec`` 轮次，把上报权
留给当前 owner。等 owner 在下一次 ``continue`` 后完成
``DELIVER`` 并打开 gate，等待中的 CPU 才能在后续调度中竞争 gate，
从而保证 GDB 一次只收到一个 hart 的 stop packet。

这套状态没有放进异常产生点，也没有放进 target env。它只存在于
TCG 统一异常处理层，作用范围是“多个 vCPU 竞争 GDB 上报权”。

验证场景
--------

仓库中包含一个轻量级验证脚本：

.. code-block:: text

   tests/qemu-gdb/riscv_gdb_exception_redirect.py

脚本使用 ``-smp 4`` 和 ``-accel tcg,thread=multi`` 验证两个原生
RISC-V 异常场景：

* ``store-fault``：固件执行 ``sd zero, 0(zero)``，触发
  ``RISCV_EXCP_STORE_ACCESS_FAULT``，异常编号 7。
* ``null-load``：固件执行 ``ld t0, 0(zero)``，触发
  ``RISCV_EXCP_LOAD_ACCESS_FAULT``，异常编号 5。

两个 demo 都先把 ``mtvec`` 指向本地自旋代码。GDB ``continue`` 后，
异常会按原有 ``do_interrupt()`` 交付给 guest，guest trap handler
随后进入自旋，避免重复产生新的异常。

典型验证命令：

.. code-block:: sh

   tests/qemu-gdb/riscv_gdb_exception_redirect.py \
       --qemu build/qemu-system-riscv64 \
       --timeout 12 \
       --smp 4 \
       --scenario store-fault \
       --qemu-log /tmp/qemu-riscv-gdb-store.log \
       --trace-gdbstub

   tests/qemu-gdb/riscv_gdb_exception_redirect.py \
       --qemu build/qemu-system-riscv64 \
       --timeout 12 \
       --smp 4 \
       --scenario null-load \
       --qemu-log /tmp/qemu-riscv-gdb-null.log \
       --trace-gdbstub

期望结果是每个场景都正好看到 4 次 ``SIGTRAP`` stop，并覆盖 4 个
不同 GDB thread。no-attach 场景下，日志中不应出现
``cpu_handle_exception: redirect exception``。
