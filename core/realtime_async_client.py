import sys
import os
import time
import ctypes
import random
import asyncio
from datetime import datetime
from loguru import logger
from pymodbus.exceptions import ModbusException
from config import settings
from pathlib import Path
import win32api
import win32con
import win32process
import traceback
from .realtime_async_connection import AsyncModbusConnection

#Windows API 常量定义
PROCESS_ALL_ACCESS = 0x1F0FFF
REALTIME_PRIORITY_CLASS = 0x00000100
HIGH_PRIORITY_CLASS = 0x00000080

THREAD_PRIORITY_TIME_CRITICAL = 15
THREAD_PRIORITY_HIGHEST = 2
THREAD_PRIORITY_NORMAL = 0

MAX_CYCLE_THRESHOLD = 0.020  # 20ms阈值

class HighPrecisionAsyncModbusClient:
    """异步Modbus客户端(Windows环境实时)"""
    def __init__(self):
        self.pool = AsyncModbusConnection()
        self._init_clock()
        self._init_realtime()
        self._stats_init()

        self._stats_lock = asyncio.Lock()

        """查找异常报文的日志(暂注)"""
        # self.errors_dir = Path("errors")
        # self.errors_dir.mkdir(exist_ok=True)
        #
        # # 初始化异常日志文件
        # timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # self.anomaly_log_path = self.errors_dir / f"cycle_anomalies_{timestamp}.log"
        #
        # # 写入日志头
        # with open(self.anomaly_log_path, "w", encoding="utf-8") as f:
        #     f.write("=== 周期异常日志 ===\n")
        #     f.write(f"开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        #     f.write("格式: [时间戳] 周期(ms) | 平均周期 | 最大周期 | 抖动 | 调用栈\n")
        #     f.write("-" * 80 + "\n")


    def _stats_init(self):
        self.stats = {
            "总请求数": 0,
            "成功请求": 0,
            "失败请求": 0,
            "开始时间": self._clock(),
            "延迟记录": [],
            "周期记录": [],
            "周期统计": {
                "平均周期": 0.0,
                "最大周期": 0.0,
                "最小周期": float('inf'),
                "周期抖动": 0.0
            },
            "报文延迟统计": {
                "read_input_registers": [],
                "read_holding_registers": [],
                "write_registers": [],
                "所有报文": []
            },
            "延迟百分位": {
               "p50": 0.0,
                "p95": 0.0,
                "p99": 0.0,
                "最大值": 0.0,
                "最小值": float('inf')
            }
        }

        self.conn_stats = {}

    def _init_clock(self):
        """初始化高精度时钟源"""
        if hasattr(time, 'perf_counter'):
            self._clock = time.perf_counter
        else:
            self._kernel32 = ctypes.windll.kernel32
            self._qpc_freq = ctypes.c_int64()
            self._kernel32.QueryPerformanceFrequency(ctypes.byref(self._qpc_freq))
            self._clock = self._qpc_counter


    def _qpc_counter(self):
        """Windows高精度计时器"""
        counter = ctypes.c_int64()
        self._kernel32.QueryPerformanceCounter(ctypes.byref(counter))
        return counter.value / self._qpc_freq.value


    def _init_realtime(self):
        """Windows实时环境初始化"""
        if sys.platform != "win32":
            return

        # 1. 提高系统时钟精度
        self._winmm = ctypes.WinDLL('winmm')
        self._winmm.timeBeginPeriod(1)
        logger.debug("系统时钟精度已设置为1ms")

        # 2. 设置进程优先级
        if settings.REALTIME_PRIORITY:
            self._set_process_priority(REALTIME_PRIORITY_CLASS)

        # 3. 设置线程优先级
        if settings.REALTIME_PRIORITY:
            try:
                self._set_thread_priority(THREAD_PRIORITY_TIME_CRITICAL)
            except RuntimeError:
                # 如果设置最高优先级失败，尝试设置高优先级
                logger.warning("无法设置TIME_CRITICAL优先级，尝试设置HIGHEST优先级")
                self._set_thread_priority(THREAD_PRIORITY_HIGHEST)

        # 4. CPU亲和性设置
        if hasattr(settings, 'REALTIME_CPU_CORE') and settings.REALTIME_CPU_CORE >= 0:
            try:
                self._set_cpu_affinity(settings.REALTIME_CPU_CORE)
                logger.success(f"成功绑定到CPU核心 {settings.REALTIME_CPU_CORE}")
            except Exception as e:
                logger.error(f"CPU绑定失败: {e}")


    def _set_process_priority(self, priority_class):
        """进程优先级设置"""
        try:
            # 正确获取当前进程ID
            pid = win32api.GetCurrentProcessId()

            # 使用win32api打开进程（替代ctypes直接调用）
            handle = win32api.OpenProcess(
                win32con.PROCESS_ALL_ACCESS,  # 使用标准权限
                False,  # 不继承句柄
                pid
            )

            # 设置优先级
            win32process.SetPriorityClass(handle, priority_class)

            # 立即关闭句柄防止泄漏
            win32api.CloseHandle(handle)

            logger.success(f"成功设置进程优先级: {self._priority_name(priority_class)}")
        except Exception as e:
            logger.error(f"优先级设置失败: {e}")
            raise RuntimeError("进程优先级设置失败") from e


    def _priority_name(self, class_code):
        """将优先级代码转为可读名称"""
        return {
            REALTIME_PRIORITY_CLASS: "实时",
            HIGH_PRIORITY_CLASS: "高",
            win32con.NORMAL_PRIORITY_CLASS: "正常"
        }.get(class_code, f"未知({class_code:#x})")


    def _set_thread_priority(self, priority_level=THREAD_PRIORITY_TIME_CRITICAL):
        """设置当前线程优先级"""
        try:
            # 使用 win32api 替代 ctypes 直接调用
            thread_handle = win32api.GetCurrentThread()

            # 设置线程优先级
            win32process.SetThreadPriority(thread_handle, priority_level)

            logger.success(f"线程优先级已设置为: {self._thread_priority_name(priority_level)}")
        except Exception as e:
            logger.error(f"线程优先级设置失败: {e}")
            raise RuntimeError("线程优先级设置失败") from e

    def _thread_priority_name(self, priority_level):
        """将线程优先级代码转为可读名称"""
        return {
            THREAD_PRIORITY_TIME_CRITICAL: "TIME_CRITICAL(15)",
            THREAD_PRIORITY_HIGHEST: "HIGHEST(2)",
            THREAD_PRIORITY_NORMAL: "NORMAL(0)"
        }.get(priority_level, f"未知({priority_level})")

    def _set_cpu_affinity(self, core_id):
        """设置CPU亲和性"""
        try:
            pid = win32api.GetCurrentProcessId()
            hProcess = win32api.OpenProcess(
                win32con.PROCESS_ALL_ACCESS,
                False,
                pid
            )

            # 获取当前亲和性掩码
            old_mask = win32process.GetProcessAffinityMask(hProcess)[0]

            # 设置新亲和性
            new_mask = 1 << core_id
            win32process.SetProcessAffinityMask(hProcess, new_mask)

            # 验证设置
            current_mask = win32process.GetProcessAffinityMask(hProcess)[0]
            if current_mask != new_mask:
                raise RuntimeError(f"CPU亲和性设置失败 (当前: {bin(current_mask)}, 预期: {bin(new_mask)})")

        except Exception as e:
            logger.error(f"CPU核心绑定异常: {e}")
            raise
        finally:
            if 'hProcess' in locals():
                win32api.CloseHandle(hProcess)

    # def _control_cycle_timing(self, cycle_start):
    #     """混合精度周期控制(C扩展)"""
    #     target_cycle = 1.0 / settings.TARGET_FREQUENCY
    #     elapsed = self._clock() - cycle_start
    #     remaining = max(0, target_cycle - elapsed)
    #
    #     if remaining > 0.002:  # >2ms使用混合等待
    #         time.sleep(remaining * 0.8)  # 80%时间释放CPU
    #         end_time = cycle_start + target_cycle
    #         while self._clock() < end_time:  # 20%忙等待
    #             pass
    #     else:  # ≤2ms纯忙等待
    #         end_time = cycle_start + target_cycle
    #         if hasattr(self, '_rtlib'):  # 使用C扩展优化
    #             self._rtlib.precise_wait_us(int(remaining * 1e6))
    #         else:
    #             while self._clock() < end_time:
    #                 pass

    # def _control_cycle_timing(self, cycle_start):
    #     """纯忙等待实现"""
    #     target_cycle = 1.0 / settings.TARGET_FREQUENCY  # 计算目标周期时间(秒)
    #     elapsed = self._clock() - cycle_start
    #     remaining = max(0, target_cycle - elapsed)
    #
    #     # 纯忙等待实现
    #     end_time = cycle_start + target_cycle
    #     while self._clock() < end_time:
    #         pass

    def _control_cycle_timing(self, cycle_start):
        """混合精度周期控制（纯Python实现）"""
        target_cycle = 1.0 / settings.TARGET_FREQUENCY
        elapsed = self._clock() - cycle_start
        remaining = max(0, target_cycle - elapsed)

        if remaining > 0:
            # 分级等待策略
            if remaining > 0.01:  # >10ms
                time.sleep(remaining * 0.9)  # 90%时间Sleep
                self._busy_wait(remaining * 0.1)  # 最后10%忙等待
            elif remaining > 0.002:  # 2-10ms
                time.sleep(remaining * 0.7)
                self._busy_wait(remaining * 0.3)
            else:  # <2ms纯忙等待
                self._busy_wait(remaining)

    def _busy_wait(self, duration):
        """优化的忙等待"""
        end_time = self._clock() + duration
        while self._clock() < end_time:
            if end_time - self._clock() > 0.001:  # >1ms剩余时短暂释放
                time.sleep(0.0001)  # 100μs级释放

    def _high_precision_wait(self, cycle_start):
        """混合精度周期控制"""
        target_cycle = 1.0 / settings.TARGET_FREQUENCY
        elapsed = self._clock() - cycle_start
        remaining = max(0, target_cycle - elapsed)

        if remaining > 0.002:  # >2ms
            time.sleep(remaining * 0.8)  # 80%时间休眠
            self._spin_wait(remaining * 0.2)  # 20%忙等待
        else:  # ≤2ms
            self._spin_wait(remaining)

    def _spin_wait(self, duration):
        """优化的忙等待"""
        end = self._clock() + duration
        while self._clock() < end:
            pass

    def _record_cycle_anomaly(self, cycle_time):
        """实时记录异常周期到独立文件"""
        try:
            cycle_ms = cycle_time * 1000
            if cycle_ms > 20:  # 超过20ms记录
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

                # 获取简化调用栈
                stack = []
                for frame in traceback.extract_stack()[:-4]:  # 跳过最后4个内部帧
                    if "site-packages" not in frame.filename:  # 过滤第三方库
                        stack.append(f"{frame.filename}:{frame.lineno} ({frame.name})")

                # 准备日志条目
                stats = self.stats["周期统计"]
                log_entry = (
                        f"[{timestamp}] {cycle_ms:.3f}ms | "
                        f"avg={stats['平均周期']:.3f}ms | "
                        f"max={stats['最大周期']:.3f}ms | "
                        f"jitter={stats['周期抖动']:.3f}ms\n"
                        f"调用栈:\n  " + "\n  ".join(stack[-3:]) + "\n"
                        f"{'-' * 40}\n"
                )

                # 实时写入文件并打印
                with open(self.anomaly_log_path, "a", encoding="utf-8") as f:
                    f.write(log_entry)

                # 立即输出到控制台
                print(f"\n! 周期异常 {cycle_ms:.1f}ms !", end="", flush=True)
                return True
        except Exception as e:
            print(f"\n记录异常失败: {str(e)}", file=sys.stderr)
        return False

    async def _random_operation(self, client):
        """执行随机Modbus操作（修正版）"""
        op_type = random.randint(0, 2)
        addr = random.randint(*settings.HOLDING_REGISTER_RANGE)
        count = min(random.randint(1, 10), settings.MAX_REGISTERS_PER_READ)

        try:
            start = self._clock()
            latency_key = ""

            if op_type == 0:
                await client.read_input_registers(address=addr, count=count)
                latency_key = "read_input_registers"
            elif op_type == 1:
                await client.read_holding_registers(address=addr, count=count)
                latency_key = "read_holding_registers"
            else:
                values = [random.randint(0, 65535) for _ in range(count)]
                await client.write_registers(address=addr, values=values)
                latency_key = "write_registers"

            latency_ms = (self._clock() - start) * 1000

            # 记录详细延迟
            self.stats["报文延迟统计"][latency_key].append(latency_ms)
            self.stats["报文延迟统计"]["所有报文"].append(latency_ms)

            # 更新全局延迟统计
            self._update_latency_stats(latency_ms)

            self.stats["成功请求"] += 1
            return True

        except ModbusException as e:
            logger.error(f"Modbus操作失败: {e}")
            self.stats["失败请求"] += 1
            return False
        finally:
            self.stats["总请求数"] += 1

    def _update_latency_stats(self, latency_ms):
        """更新延迟百分位统计"""
        stats = self.stats["延迟百分位"]
        stats["最大值"] = max(stats["最大值"], latency_ms)
        stats["最小值"] = min(stats["最小值"], latency_ms)

    def _calculate_percentiles(self, data):
        """计算百分位延迟"""
        if not data:
            return 0.0, 0.0, 0.0

        sorted_data = sorted(data)
        n = len(sorted_data)

        p50 = sorted_data[int(n * 0.50)]
        p95 = sorted_data[int(n * 0.95)]
        p99 = sorted_data[int(n * 0.99)]

        return p50, p95, p99

    def _analyze_latencies(self):
        """分析所有延迟数据"""
        all_latencies = self.stats["报文延迟统计"]["所有报文"]
        if not all_latencies:
            return

        # 计算百分位
        p50, p95, p99 = self._calculate_percentiles(all_latencies)

        self.stats["延迟百分位"].update({
            "p50": p50,
            "p95": p95,
            "p99": p99,
            "最大值": max(all_latencies),
            "最小值": min(all_latencies)
        })

        # 各操作类型的平均延迟
        for op_type in ["read_input_registers", "read_holding_registers", "write_registers"]:
            latencies = self.stats["报文延迟统计"][op_type]
            if latencies:
                avg = sum(latencies) / len(latencies)
                self.stats["报文延迟统计"][f"{op_type}_平均"] = avg

    def _update_cycle_stats(self, cycle_time):
        """更新周期统计数据"""
        cycle_ms = cycle_time * 1000
        self.stats["周期记录"].append(cycle_ms)

        # 统计异常周期(暂注)
        # if cycle_ms > 20:
        #     self._record_cycle_anomaly(cycle_time)

        cycles = self.stats["周期记录"]
        stats = self.stats["周期统计"]
        stats["平均周期"] = sum(cycles) / len(cycles)
        stats["最大周期"] = max(cycles)
        stats["最小周期"] = min(cycles)

        recent = cycles[-100:] if len(cycles) >= 100 else cycles
        if len(recent) > 1:
            mean = sum(recent) / len(recent)
            variance = sum((x - mean)**2 for x in recent) / (len(recent)-1)
            stats["周期抖动"] = variance ** 0.5

    def _print_cycle_stats(self):
        """打印周期统计信息"""
        stats = self.stats["周期统计"]
        print(
            f"\r--- 周期统计 --- "
            f"平均周期: {stats['平均周期']:.6f}ms | "
            f"最大周期: {stats['最大周期']:.6f}ms | "
            f"最小周期: {stats['最小周期']:.6f}ms | "
            f"周期抖动: {stats['周期抖动']:.6f}ms",
            end=""
        )
        """连接预热方法"""

    async def _warmup(self, connections):
        logger.info("开始连接预热...")
        warmup_start = self._clock()

        # 预热期间不记录统计信息
        while self._clock() < warmup_start + 1.0:  # 预热1秒
            await asyncio.gather(*[
                self._cycle_operation(conn, record_stats=False)
                for conn in connections
            ])

        logger.success(f"预热完成，耗时 {(self._clock() - warmup_start) * 1000:.2f}ms")

    async def _reconnect(self, old_conn):
        """增强版重连逻辑"""
        try:
            # 安全关闭旧连接
            if old_conn is not None:
                try:
                    if hasattr(old_conn, 'close'):
                        await asyncio.wait_for(old_conn.close(), timeout=1.0)
                except Exception as e:
                    logger.warning(f"关闭旧连接异常: {e}")

            # 创建新连接
            new_conn = await self.pool.get_connection()

            # 验证连接有效性
            if not hasattr(new_conn, 'host') or not new_conn.connected:
                raise ConnectionError("新连接无效")

            return new_conn

        except Exception as e:
            logger.critical(f"重连失败: {e}")
            await asyncio.sleep(1)  # 避免快速重试
            raise ConnectionError(f"重连失败: {e}") from e

    async def _safe_close_connections(self, connections):
        """安全关闭连接集合"""
        if not connections:
            return

        await asyncio.gather(
            *[self._safe_close(c) for c in connections],
            return_exceptions=True
        )

    async def _safe_close(self, client):
        """安全关闭单个连接"""
        if client is None:
            return

        try:
            if hasattr(client, 'close'):
                await asyncio.wait_for(client.close(), timeout=1.0)
        except Exception as e:
            logger.warning(f"关闭连接异常: {e}")

    def _update_conn_stats(self, conn_name, cycle_time, success):
        """更新连接级统计"""
        if conn_name not in self.conn_stats:
            self.conn_stats[conn_name] = {
                "total": 0,
                "success": 0,
                "cycles": [],
                "last_active": self._clock()
            }

        stats = self.conn_stats[conn_name]
        stats["total"] += 1
        stats["success"] += int(success)
        stats["cycles"].append(cycle_time)
        stats["last_active"] = self._clock()

    async def _connection_cycle(self, conn, conn_name):
        """单个连接的完整工作周期"""
        try:
            async with self._stats_lock:
                start_time = self._clock()
                # 执行Modbus操作
                success = await self._random_operation(conn)
                cycle_time = self._clock() - start_time
                self._update_conn_stats(conn_name, cycle_time, success)

        except Exception as e:
            logger.error(f"{conn_name} 操作失败: {e}")
            raise

    async def run_test(self, duration):
        """多连接并行压力测试"""
        logger.info(f"启动压力测试(连接数:{settings.CONNECTION_POOL_SIZE})...")
        end_time = self._clock() + duration

        # 初始化连接池并打印信息
        connections = []
        for i in range(settings.CONNECTION_POOL_SIZE):
            try:
                conn = await self.pool.get_connection()
                connections.append(conn)
                logger.success(f"连接{i+1}建立 | {self._get_conn_info(conn)}")
            except Exception as e:
                logger.error(f"初始化连接{i+1}失败: {e}")
                raise

        # 预热
        await self._warmup(connections)

        # 主循环
        try:
            while self._clock() < end_time:
                cycle_start = self._clock()

                # 为每个连接创建独立任务
                tasks = []
                for i, conn in enumerate(connections):
                    task = asyncio.create_task(
                        self._connection_cycle(conn, f"conn_{i + 1}"),
                        name=f"modbus_worker_{i}"
                    )
                    tasks.append(task)

                # 等待所有连接完成本轮操作
                await asyncio.gather(*tasks)

                # 精确周期控制
                self._high_precision_wait(cycle_start)

                # 打印状态
                if len(self.stats["周期记录"]) % 100 == 0:
                    self._print_cycle_stats()

        finally:
            await self._safe_close_connections(connections)
            self._generate_report()

    def _get_conn_info(self, client):
        """获取连接信息"""
        try:
            if hasattr(client, 'comm_params'):
                return f"{client.comm_params.host}:{client.comm_params.port}"
            return "unknown_connection"
        except Exception:
            return "connection_info_error"

    async def _cycle_operation(self, client, record_stats=True):
        """单连接周期操作"""
        cycle_start = self._clock()
        try:
            async with asyncio.timeout(0.5):  # 500ms操作超时
                await self._random_operation(client)

            if record_stats:
                cycle_time = self._clock() - cycle_start
                self._update_cycle_stats(cycle_time)

                # 记录异常周期
                if cycle_time * 1000 > 20:
                    self._record_anomaly(client, cycle_time)

        except asyncio.TimeoutError:
            logger.warning(f"连接 {client.host}:{client.port} 操作超时")
            raise
        except Exception as e:
            logger.warning(f"连接{client.host}:{client.port}操作失败: {str(e)}")
            raise

    def _record_anomaly(self, client, cycle_time):
        """记录带连接信息的异常"""
        log_entry = (
            f"\n=== 连接异常 ===\n"
            f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')}\n"
            f"连接: {client.host}:{client.port}\n"
            f"异常周期: {cycle_time * 1000:.3f}ms\n"
            f"Socket状态: {client.socket.getsockname()}\n"
        )
        with open("connection_anomalies.log", "a", encoding="utf-8") as f:
            f.write(log_entry)

    def _generate_report(self):
        """生成包含延迟统计的详细报告"""
        # 先分析延迟数据
        self._analyze_latencies()

        # 准备报告内容
        duration = self._clock() - self.stats["开始时间"]
        qps = self.stats["总请求数"] / duration

        report_lines = [
            "=== Modbus异步测试报告 ===",
            f"测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"运行时长: {duration:.2f}秒",
            f"总请求数: {self.stats['总请求数']}",
            f"成功请求: {self.stats['成功请求']}",
            f"失败请求: {self.stats['失败请求']}",
            f"QPS: {qps:.2f}",
            f"成功率: {(self.stats['成功请求'] / self.stats['总请求数']) * 100:.2f}%",
            "",
            "=== 周期统计 ===",
            f"平均周期: {self.stats['周期统计']['平均周期']:.6f}ms",
            f"最大周期: {self.stats['周期统计']['最大周期']:.6f}ms",
            f"最小周期: {self.stats['周期统计']['最小周期']:.6f}ms",
            f"周期抖动: {self.stats['周期统计']['周期抖动']:.6f}ms",
            "",
            "=== 报文延迟统计 ===",
            f"总报文数: {len(self.stats['报文延迟统计']['所有报文'])}",
            f"平均延迟: {sum(self.stats['报文延迟统计']['所有报文']) / len(self.stats['报文延迟统计']['所有报文']):.3f}ms",
            f"P50延迟: {self.stats['延迟百分位']['p50']:.3f}ms",
            f"P95延迟: {self.stats['延迟百分位']['p95']:.3f}ms",
            f"P99延迟: {self.stats['延迟百分位']['p99']:.3f}ms",
            f"最大延迟: {self.stats['延迟百分位']['最大值']:.3f}ms",
            f"最小延迟: {self.stats['延迟百分位']['最小值']:.3f}ms",
            "",
            "=== 各操作类型延迟 ===",
            f"读输入寄存器平均: {self.stats['报文延迟统计'].get('read_input_registers_平均', 0):.3f}ms (样本数: {len(self.stats['报文延迟统计']['read_input_registers'])})",
            f"读保持寄存器平均: {self.stats['报文延迟统计'].get('read_holding_registers_平均', 0):.3f}ms (样本数: {len(self.stats['报文延迟统计']['read_holding_registers'])})",
            f"写寄存器平均: {self.stats['报文延迟统计'].get('write_registers_平均', 0):.3f}ms (样本数: {len(self.stats['报文延迟统计']['write_registers'])})"
        ]

        report_content = "\n".join(report_lines)

        # 写入UTF-8文件
        report_dir = Path(r"E:\QJRobot\Source Code\qj-pytest\modbus_stress\reports")
        report_dir.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = report_dir / f"modbus_test_{timestamp}.txt"

        try:
            with open(report_path, 'w', encoding='utf-8') as f:
                f.write(report_content)
            logger.info(f"测试报告已保存至: {report_path}")
        except Exception as e:
            logger.error(f"保存测试报告失败: {e}")

        # 控制台输出
        print(report_content)

    async def cleanup(self):
        """终极安全清理"""
        cleanup_errors = 0

        # 1. 确保测试报告生成
        try:
            self._generate_report()
        except Exception as e:
            logger.error(f"生成报告失败: {type(e).__name__}")
            cleanup_errors += 1

        # 2. 恢复时钟精度
        if hasattr(self, '_winmm'):
            try:
                self._winmm.timeEndPeriod(1)
                logger.debug("系统时钟精度已恢复")
            except Exception as e:
                logger.error(f"恢复时钟精度失败: {type(e).__name__}")
                cleanup_errors += 1
            finally:
                self._winmm = None

        # 3. 关闭连接池
        if hasattr(self, 'pool') and self.pool:
            try:
                # 添加超时保护
                await asyncio.wait_for(self.pool.close_all(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.error("关闭连接池超时")
                cleanup_errors += 1
            except Exception as e:
                logger.error(f"关闭连接池失败: {type(e).__name__}")
                cleanup_errors += 1
            finally:
                self.pool = None

        # 4. 清理统计信息
        self.stats.clear()

        if cleanup_errors > 0:
            logger.warning(f"清理完成，但有{cleanup_errors}个错误")
        else:
            logger.info("所有资源已安全释放")
