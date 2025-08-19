import sys
import os
import time
import ctypes
import random
import asyncio
from datetime import datetime
from loguru import logger
from pymodbus.exceptions import ModbusException, ConnectionException
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

class HighPrecisionAsyncModbusClient:
    """异步Modbus客户端(Windows环境实时)"""
    def __init__(self, master_ids):
        self.master_ids = master_ids
        self.pool = AsyncModbusConnection()
        self._init_clock()
        self._init_realtime()
        self.client_stats = {}
        self.stats_lock = asyncio.Lock()
        self.last_response_time = {}  # 新增响应时间记录

        # 为每个客户端初始化统计
        for master_id in master_ids:
            self._init_client_stats(master_id)
            self.last_response_time[master_id] = None

        # 初始化全局统计
        self.global_stats = {
            "start_time": self._clock(),
            "total_requests": 0,
            "success_requests": 0,
            "failed_requests": 0,
            "timeout_requests": 0
        }

    def _init_client_stats(self, master_id):
        """为每个客户端初始化统计信息"""
        self.client_stats[master_id] = {
            "总请求数": 0,
            "成功请求": 0,
            "失败请求": 0,
            "超时请求": 0,
            "开始时间": self._clock(),
            "轮询周期记录": [],
            "报文延迟记录": [],
            "轮询周期统计": {
                "平均轮询周期": 0.0,
                "最大轮询周期": 0.0,
                "最小轮询周期": float('inf'),
                "轮询周期抖动": 0.0
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

    async def _random_operation(self, client, master_id):
        """执行随机Modbus操作（精确轮询周期统计）"""
        # 记录操作开始时间（用于计算延迟）
        operation_start = self._clock()
        poll_cycle_recorded = False

        try:
            # 检查是否有上一次响应时间，并计算轮询周期间隔
            async with self.stats_lock:
                if hasattr(self, 'last_response_time') and master_id in self.last_response_time and \
                        self.last_response_time[master_id] is not None:
                    poll_interval = (operation_start - self.last_response_time[master_id]) * 1000  # ms
                    self.client_stats[master_id]["轮询周期记录"].append(poll_interval)
                    poll_cycle_recorded = True

        except Exception as e:
            logger.warning(f"轮询周期统计异常: {e}")

        op_type = random.randint(0, 2)
        addr = random.randint(*settings.HOLDING_REGISTER_RANGE)
        count = min(random.randint(1, 10), settings.MAX_REGISTERS_PER_READ)

        try:
            start = self._clock()
            latency_key = ""
            response_time = None

            try:
                # 执行操作并记录响应时间
                if op_type == 0:
                    result = await asyncio.wait_for(
                        client.read_input_registers(address=addr, count=count),
                        timeout=settings.RESPONSE_TIMEOUT
                    )
                    latency_key = "read_input_registers"
                elif op_type == 1:
                    result = await asyncio.wait_for(
                        client.read_holding_registers(address=addr, count=count),
                        timeout=settings.RESPONSE_TIMEOUT
                    )
                    latency_key = "read_holding_registers"
                else:
                    values = [random.randint(0, 65535) for _ in range(count)]
                    result = await asyncio.wait_for(
                        client.write_registers(address=addr, values=values),
                        timeout=settings.RESPONSE_TIMEOUT
                    )
                    latency_key = "write_registers"

                # 记录响应完成时间
                response_time = self._clock()

            except asyncio.TimeoutError:
                # 记录超时请求
                async with self.stats_lock:
                    self.client_stats[master_id]["超时请求"] += 1
                    self.global_stats["timeout_requests"] += 1
                    self.client_stats[master_id]["总请求数"] += 1

                logger.error(f"客户端 [{master_id}] 操作超时")
                return False

            # 计算报文延迟
            if response_time is not None:
                latency_ms = (response_time - start) * 1000

                # 更新统计信息
                async with self.stats_lock:
                    # 更新客户端统计
                    stats = self.client_stats[master_id]
                    stats["报文延迟统计"][latency_key].append(latency_ms)
                    stats["报文延迟统计"]["所有报文"].append(latency_ms)
                    stats["成功请求"] += 1
                    stats["总请求数"] += 1

                    # 更新响应时间
                    self.last_response_time[master_id] = response_time

                    # 更新全局统计
                    self.global_stats["total_requests"] += 1
                    self.global_stats["success_requests"] += 1

            return True

        except ModbusException as e:
            logger.error(f"Modbus操作失败: {e}")
            async with self.stats_lock:
                self.client_stats[master_id]["失败请求"] += 1
                self.client_stats[master_id]["总请求数"] += 1
                self.global_stats["total_requests"] += 1
                self.global_stats["failed_requests"] += 1
            return False
        except ConnectionException as e:
            logger.error(f"连接异常: {e}")
            async with self.stats_lock:
                self.client_stats[master_id]["失败请求"] += 1
                self.client_stats[master_id]["总请求数"] += 1
                self.global_stats["total_requests"] += 1
                self.global_stats["failed_requests"] += 1
            return False
        except Exception as e:
            logger.error(f"操作异常: {e}")
            async with self.stats_lock:
                self.client_stats[master_id]["失败请求"] += 1
                self.client_stats[master_id]["总请求数"] += 1
                self.global_stats["total_requests"] += 1
                self.global_stats["failed_requests"] += 1
            return False

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

    def _update_poll_cycle_stats(self, master_id):
        """更新单个客户端的轮询周期统计信息"""
        stats = self.client_stats[master_id]
        cycles = stats["轮询周期记录"]
        if not cycles:
            return

        cycle_stats = stats["轮询周期统计"]
        cycle_stats["平均轮询周期"] = sum(cycles) / len(cycles)
        cycle_stats["最大轮询周期"] = max(cycles)
        cycle_stats["最小轮询周期"] = min(cycles)

        # 计算抖动（标准差）
        if len(cycles) > 1:
            mean = cycle_stats["平均轮询周期"]
            variance = sum((x - mean) ** 2 for x in cycles) / (len(cycles) - 1)
            cycle_stats["轮询周期抖动"] = variance ** 0.5
        else:
            cycle_stats["轮询周期抖动"] = 0.0

    def _get_client_poll_cycle_stats_str(self, master_id):
        """获取单个客户端的轮询周期统计字符串"""
        self._update_poll_cycle_stats(master_id)
        stats = self.client_stats[master_id]["轮询周期统计"]

        formatted_avg = f"{stats['平均轮询周期']:.3f}".rjust(6)
        formatted_max = f"{stats['最大轮询周期']:.3f}".rjust(6)
        formatted_jitter = f"{stats['轮询周期抖动']:.3f}".rjust(6)

        return f"[客户端 {master_id}] 平均轮询周期: {formatted_avg}ms | 最大轮询周期: {formatted_max}ms | 抖动: {formatted_jitter}ms"

    def print_all_client_poll_cycle_stats(self):
        """打印所有客户端的轮询周期统计信息"""
        # 清屏或移动光标到行首
        print("\033[F" * (len(self.master_ids) + 1), end="")  # 移动光标到上一行

        # 打印所有客户端统计
        for master_id in self.master_ids:
            print(self._get_client_poll_cycle_stats_str(master_id))

        # 添加空行使输出更清晰
        print("")

    # async def _warmup(self, connections):
    #     """连接预热方法"""
    #     logger.info("开始连接预热...")
    #     warmup_start = self._clock()
    #
    #     # 预热期间不记录统计信息
    #     while self._clock() < warmup_start + 1.0:  # 预热1秒
    #         await asyncio.gather(*[
    #             self._cycle_operation(conn, record_stats=False)
    #             for conn in connections
    #         ])
    #
    #     logger.success(f"预热完成，耗时 {(self._clock() - warmup_start) * 1000:.2f}ms")

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

    async def _client_loop(self, conn, master_id, cycle_time, end_time):
        """改进的精确周期控制循环（抗累积漂移）"""
        iteration_count = 0
        base_time = self._clock()  # 基准时间

        while self._clock() < end_time:
            iteration_count += 1

            # 计算绝对目标时间（消除累积误差）
            target_time = base_time + (iteration_count * cycle_time)
            now = self._clock()

            # 精确等待
            if now < target_time:
                remaining = target_time - now

                # 分级等待
                if remaining > 0.002:
                    await asyncio.sleep(remaining * 0.7)
                    # 忙等待剩余时间
                    bus_start = self._clock()
                    while self._clock() < target_time:
                        pass
                else:
                    # 纯忙等待
                    while self._clock() < target_time:
                        pass

            # 执行操作
            await self._random_operation(conn, master_id)

            # 每10次迭代打印统计
            if iteration_count % 10 == 0:
                async with self.stats_lock:
                    self.print_all_client_poll_cycle_stats()

        # 重置基准时间避免浮点精度累积
        base_time = self._clock()

    async def run_test(self, duration):
        """多连接并行压力测试"""
        logger.info(f"启动压力测试(客户端数:{len(self.master_ids)})...")
        end_time = self._clock() + duration

        # 为每个客户端创建连接
        connections = {}
        for master_id in self.master_ids:
            try:
                conn = await self.pool.get_connection()
                connections[master_id] = conn
                logger.success(f"客户端 [{master_id}] 连接建立 | {self._get_conn_info(conn)}")
            except Exception as e:
                logger.error(f"客户端 [{master_id}] 初始化失败: {e}")
                raise

        # 预热
        # await self._warmup(connections)

        # 打印初始统计信息
        print("\n" * len(self.master_ids))  # 预留空间

        # 为每个客户端创建独立任务
        tasks = []
        for master_id, conn in connections.items():
            # 获取该客户端的周期配置
            config = settings.MASTER_CONFIGS.get(master_id, {})
            cycle_time = config.get("cycle_time") or 1.0 / settings.TARGET_FREQUENCY

            # 创建任务
            task = asyncio.create_task(
                self._client_loop(conn, master_id, cycle_time, end_time),
                name=f"modbus_worker_{master_id}"
            )
            tasks.append(task)

        # 等待所有任务完成或超时
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("测试被取消")
        except Exception as e:
            logger.error(f"测试运行失败: {e}")
        finally:
            await self._safe_close_connections(list(connections.values()))
            self._generate_report()

    def _get_conn_info(self, client):
        """获取连接信息"""
        try:
            if hasattr(client, 'comm_params'):
                return f"{client.comm_params.host}:{client.comm_params.port}"
            return "unknown_connection"
        except Exception:
            return "connection_info_error"

    # async def _cycle_operation(self, client, record_stats=True):
    #     """单连接周期操作"""
    #     cycle_start = self._clock()
    #     try:
    #         async with asyncio.timeout(0.5):  # 500ms操作超时
    #             await self._random_operation(client)
    #
    #         if record_stats:
    #             cycle_time = self._clock() - cycle_start
    #             self._update_cycle_stats(cycle_time)
    #
    #             # 记录异常周期
    #             if cycle_time * 1000 > 20:
    #                 self._record_anomaly(client, cycle_time)
    #
    #     except asyncio.TimeoutError:
    #         logger.warning(f"连接 {client.host}:{client.port} 操作超时")
    #         raise
    #     except Exception as e:
    #         logger.warning(f"连接{client.host}:{client.port}操作失败: {str(e)}")
    #         raise

    # def _record_anomaly(self, client, cycle_time):
    #     """记录带连接信息的异常"""
    #     log_entry = (
    #         f"\n=== 连接异常 ===\n"
    #         f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')}\n"
    #         f"连接: {client.host}:{client.port}\n"
    #         f"异常周期: {cycle_time * 1000:.3f}ms\n"
    #         f"Socket状态: {client.socket.getsockname()}\n"
    #     )
    #     with open("connection_anomalies.log", "a", encoding="utf-8") as f:
    #         f.write(log_entry)

    def _generate_report(self):
        """生成包含所有客户端统计的详细报告（更新为轮询周期统计）"""
        # 准备报告头部
        report_lines = [
            "=== Modbus多客户端测试报告 ===",
            f"测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "=== 全局统计 ==="
        ]

        # 全局统计
        total_requests = self.global_stats["total_requests"]
        total_success = self.global_stats["success_requests"]
        total_failures = self.global_stats["failed_requests"]
        total_timeouts = self.global_stats["timeout_requests"]
        start_time = self.global_stats["start_time"]
        end_time = self._clock()

        duration = end_time - start_time
        qps = total_requests / duration if duration > 0 else 0
        success_rate = (total_success / total_requests * 100) if total_requests > 0 else 0

        report_lines.extend([
            f"运行时长: {duration:.2f}秒",
            f"总请求数: {total_requests}",
            f"成功请求: {total_success}",
            f"失败请求: {total_failures}",
            f"超时请求: {total_timeouts}",
            f"QPS: {qps:.2f}",
            f"成功率: {success_rate:.2f}%",
            "",
            "=== 客户端详细统计 ==="
        ])

        # 每个客户端的详细统计
        for master_id, stats in self.client_stats.items():
            config = settings.MASTER_CONFIGS.get(master_id, {})
            client_duration = self._clock() - stats["开始时间"]
            client_requests = stats["总请求数"]
            client_qps = client_requests / client_duration if client_duration > 0 else 0

            # 确保周期配置不为None
            cycle_time = config.get("cycle_time")
            if cycle_time is None:
                cycle_time = 1.0 / settings.TARGET_FREQUENCY

            report_lines.extend([
                f"\n--- 客户端 [{master_id}] ---",
                f"描述: {config.get('description', '无描述')}",
                f"设定轮询周期: {cycle_time * 1000:.3f}ms",  # 更新为轮询周期
                f"运行时长: {client_duration:.2f}秒",
                f"总请求数: {client_requests}",
                f"成功请求: {stats['成功请求']}",
                f"失败请求: {stats['失败请求']}",
                f"超时请求: {stats['超时请求']}",  # 添加超时请求统计
                f"QPS: {client_qps:.2f}",
                f"成功率: {(stats['成功请求'] / client_requests * 100) if client_requests > 0 else 0:.2f}%",
                "",
                "轮询周期统计:",  # 更新为轮询周期统计
                f"  平均轮询周期: {stats['轮询周期统计']['平均轮询周期']:.6f}ms",  # 更新字段名
                f"  最大轮询周期: {stats['轮询周期统计']['最大轮询周期']:.6f}ms",  # 更新字段名
                f"  最小轮询周期: {stats['轮询周期统计']['最小轮询周期']:.6f}ms",  # 更新字段名
                f"  轮询周期抖动: {stats['轮询周期统计']['轮询周期抖动']:.6f}ms",  # 更新字段名
                "",
                "报文延迟统计:",
            ])

            # 延迟百分位计算
            if stats['报文延迟统计']['所有报文']:
                sorted_latencies = sorted(stats['报文延迟统计']['所有报文'])
                n = len(sorted_latencies)
                p50 = sorted_latencies[int(n * 0.5)] if n > 0 else 0
                p95 = sorted_latencies[int(n * 0.95)] if n > 1 else 0
                p99 = sorted_latencies[int(n * 0.99)] if n > 2 else 0

                report_lines.append(f"  所有报文延迟: avg={sum(sorted_latencies) / n:.3f}ms, "
                                    f"p50={p50:.3f}ms, p95={p95:.3f}ms, p99={p99:.3f}ms")

            # 各操作类型延迟
            for op_type in ['read_input_registers', 'read_holding_registers', 'write_registers']:
                latencies = stats['报文延迟统计'][op_type]
                if latencies:
                    avg = sum(latencies) / len(latencies)
                    report_lines.append(f"  {op_type}: avg={avg:.3f}ms, 样本数={len(latencies)}")

        # 写入文件
        report_content = "\n".join(report_lines)
        report_dir = Path("reports")
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
            logger.error(f"生成报告失败: {type(e).__name__} - {e}")
            cleanup_errors += 1

        # 2. 恢复时钟精度
        if hasattr(self, '_winmm'):
            try:
                self._winmm.timeEndPeriod(1)
                logger.debug("系统时钟精度已恢复")
            except Exception as e:
                logger.error(f"恢复时钟精度失败: {type(e).__name__} - {e}")
                cleanup_errors += 1

        # 3. 关闭连接池
        if hasattr(self, 'pool') and self.pool:
            try:
                # 添加超时保护
                await asyncio.wait_for(self.pool.close_all(), timeout=5.0)
                logger.debug("连接池已关闭")
            except asyncio.TimeoutError:
                logger.error("关闭连接池超时")
                cleanup_errors += 1
            except Exception as e:
                logger.error(f"关闭连接池失败: {type(e).__name__} - {e}")
                cleanup_errors += 1

        if cleanup_errors > 0:
            logger.warning(f"清理完成，但有{cleanup_errors}个错误")
        else:
            logger.info("所有资源已安全释放")