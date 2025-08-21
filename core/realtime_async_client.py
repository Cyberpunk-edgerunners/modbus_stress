import sys
import os
import time
import ctypes
import random
import asyncio
from collections import deque
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

        self.last_request_sent = {}       # 请求发送时间记录
        self.last_response_received = {}  # 响应接收时间记录

        # 为每个客户端分配连接ID
        self.client_conn_ids = {
            master_id: idx for idx, master_id in enumerate(master_ids)
        }

        # 为每个客户端初始化统计
        for master_id in master_ids:
            self._init_client_stats(master_id)
            self.last_request_sent[master_id] = None
            self.last_response_received[master_id] = None

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
            "轮询周期记录": deque(maxlen=1000),
            "报文延迟记录": deque(maxlen=1000),
            "轮询周期统计": {
                "平均轮询周期": 0.0,
                "最大轮询周期": 0.0,
                "最小轮询周期": float('inf'),
                "轮询周期抖动": 0.0
            },
            "轮询周期累积": {
                "总和": 0.0,
                "计数": 0,
                "最大值": 0.0,
                "最小值": float('inf')
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
        """高精度时钟源（带缓存优化）"""
        if hasattr(time, 'perf_counter'):
            self._clock = self._cached_perf_counter
            self._last_time = 0.0
        else:
            self._kernel32 = ctypes.windll.kernel32
            self._qpc_freq = ctypes.c_int64()
            self._kernel32.QueryPerformanceFrequency(ctypes.byref(self._qpc_freq))
            self._clock = self._cached_qpc_counter
            self._last_time = 0.0

    def _cached_perf_counter(self):
        """带缓存的perf_counter实现"""
        current = time.perf_counter()
        # 每毫秒更新一次缓存
        if int(current * 1000) != int(self._last_time * 1000):
            self._last_time = current
        return self._last_time

    def _cached_qpc_counter(self):
        """带缓存的QPC实现"""
        counter = ctypes.c_int64()
        self._kernel32.QueryPerformanceCounter(ctypes.byref(counter))
        current = counter.value / self._qpc_freq.value
        # 每毫秒更新一次缓存
        if int(current * 1000) != int(self._last_time * 1000):
            self._last_time = current
        return self._last_time


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

    # 异步等待他妈的就是有问题的
    async def _async_high_precision_wait(self, wait_duration):
        """优化的异步高精度等待"""
        if wait_duration <= 0:
            return

        # 极短时直接使用忙等待（<0.5ms）
        if wait_duration < 0.0005:  # <0.5ms
            self._spin_wait(wait_duration)
            return

        # 短时等待（0.5-2ms）使用更高比例忙等待
        if wait_duration <= 0.002:
            # 50%时间异步睡眠，50%时间忙等待
            sleep_duration = wait_duration * 0.5
            spin_duration = wait_duration * 0.5
            await asyncio.sleep(sleep_duration)
            self._spin_wait(spin_duration)

        # 中长时等待（>2ms）
        else:
            # 保留0.2ms用于精确忙等待
            sleep_duration = max(0, wait_duration - 0.0002)
            spin_duration = 0.0002

            await asyncio.sleep(sleep_duration)
            self._spin_wait(spin_duration)

    def _spin_wait(self, duration):
        """优化的忙等待（增加让出控制权点）"""
        if duration <= 0:
            return

        end_time = self._clock() + duration
        check_counter = 0

        # 短时忙等待（<0.5ms）
        if duration < 0.0005:
            while self._clock() < end_time:
                pass
        # 中等时长忙等待
        elif duration < 0.001:  # 0.5ms-1ms
            while self._clock() < end_time:
                # 每500次检查让出一次（约1us）
                check_counter += 1
                if check_counter % 500 == 0:
                    time.sleep(0)  # 短暂让出控制权
        # 长时忙等待（带短暂释放）
        else:
            while self._clock() < end_time:
                # 每100次检查让出一次
                check_counter += 1
                if check_counter % 100 == 0:
                    # 每100us让出一次
                    if end_time - self._clock() > 0.0001:
                        time.sleep(0.00001)

    def _precise_wait(self, target_time):
        """高精度等待（无异步操作）"""
        current = self._clock()
        if current >= target_time:
            return

        duration = target_time - current

        # 分级等待策略
        if duration > 0.002:  # >2ms
            # 使用系统sleep处理大部分时间
            sleep_time = duration * 0.95
            time.sleep(sleep_time)
            # 剩余时间忙等待
            self._micro_spin_wait(target_time - sleep_time)
        else:
            # 短时直接忙等待
            self._micro_spin_wait(duration)

    def _micro_spin_wait(self, duration):
        """微秒级精度的忙等待"""
        if duration <= 0:
            return

        end_time = self._clock() + duration
        while self._clock() < end_time:
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
        # 记录本次请求发送时间（关键时间点）
        request_sent_time = self._clock()

        # 更新请求发送时间（用于下次计算周期）
        async with self.stats_lock:
            last_resp_time = self.last_response_received[master_id]
            self.last_request_sent[master_id] = request_sent_time

            # 只有当有上一次响应时间时才计算周期
            if last_resp_time is not None:
                # 计算真实轮询周期：本次请求发送时间 - 上次响应接收时间
                true_poll_interval = (request_sent_time - last_resp_time) * 1000

                # # 确保周期为正
                # if true_poll_interval < 0:
                #     logger.error(f"负轮询周期: {true_poll_interval:.3f}ms, "
                #                  f"请求时间={request_sent_time}, "
                #                  f"响应时间={last_resp_time}")
                #     true_poll_interval = abs(true_poll_interval)

                # 更新累积统计
                cum_stats = self.client_stats[master_id]["轮询周期累积"]
                cum_stats["总和"] += true_poll_interval
                cum_stats["计数"] += 1

                # 更新最大值和最小值
                if true_poll_interval > cum_stats["最大值"]:
                    cum_stats["最大值"] = true_poll_interval
                if true_poll_interval < cum_stats["最小值"]:
                    cum_stats["最小值"] = true_poll_interval

                # 添加到记录
                self.client_stats[master_id]["轮询周期记录"].append(true_poll_interval)

                # 检查偏差（与设定的周期比较）
                config = settings.MASTER_CONFIGS.get(master_id, {})
                target_cycle = config.get("cycle_time") or 1.0 / settings.TARGET_FREQUENCY
                target_cycle_ms = target_cycle * 1000
                deviation = abs(true_poll_interval - target_cycle_ms)

                if deviation > 20:  # 超过20ms偏差记录警告
                    logger.warning(
                        f"客户端 [{master_id}] 轮询周期偏差: "
                        f"设定={target_cycle_ms:.3f}ms, "
                        f"实际={true_poll_interval:.3f}ms, "
                        f"偏差={deviation:.3f}ms"
                    )

        # 随机选择操作类型
        op_type = random.randint(0, 2)
        addr = random.randint(*settings.HOLDING_REGISTER_RANGE)
        count = min(random.randint(1, 10), settings.MAX_REGISTERS_PER_READ)

        try:
            operation_start = self._clock()
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

                # 记录响应接收时间
                async with self.stats_lock:
                    self.last_response_received[master_id] = response_time

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
                latency_ms = (response_time - operation_start) * 1000

                # 更新统计信息
                async with self.stats_lock:
                    # 更新客户端统计
                    stats = self.client_stats[master_id]
                    stats["报文延迟统计"][latency_key].append(latency_ms)
                    stats["报文延迟统计"]["所有报文"].append(latency_ms)
                    stats["成功请求"] += 1
                    stats["总请求数"] += 1

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

    # # 下面是原始的周期监控，只统计最近100个周期的抖动，记录的是平均周期
    # # 25.8.19
    # def _update_poll_cycle_stats(self, master_id):
    #     """更新单个客户端的轮询周期统计信息"""
    #     stats = self.client_stats[master_id]
    #     cycles = stats["轮询周期记录"]
    #
    #     # 仅使用最近100次记录
    #     recent_cycles = cycles[-100:] if len(cycles) > 100 else cycles
    #
    #     if not cycles:
    #         return
    #
    #     cycle_stats = stats["轮询周期统计"]
    #     cycle_stats["平均轮询周期"] = sum(cycles) / len(cycles)
    #     cycle_stats["最大轮询周期"] = max(cycles)
    #     cycle_stats["最小轮询周期"] = min(cycles)
    #
    #     # 计算抖动（标准差）
    #     if len(recent_cycles) > 1:
    #         mean = cycle_stats["平均轮询周期"]
    #         variance = sum((x - mean) ** 2 for x in recent_cycles) / (len(recent_cycles) - 1)
    #         cycle_stats["轮询周期抖动"] = variance ** 0.5
    #     else:
    #         cycle_stats["轮询周期抖动"] = 0.0

    # 统计最近100个周期的平均周期和抖动
    # 25.8.19
    def _update_poll_cycle_stats(self, master_id):
        """高效更新轮询周期统计（使用累积变量）"""
        stats = self.client_stats[master_id]
        cum_stats = stats["轮询周期累积"]
        cycle_stats = stats["轮询周期统计"]

        # 使用累积变量计算平均值
        if cum_stats["计数"] > 0:
            cycle_stats["平均轮询周期"] = cum_stats["总和"] / cum_stats["计数"]
            cycle_stats["最大轮询周期"] = cum_stats["最大值"]
            cycle_stats["最小轮询周期"] = cum_stats["最小值"]
        else:
            cycle_stats["平均轮询周期"] = 0.0
            cycle_stats["最大轮询周期"] = 0.0
            cycle_stats["最小轮询周期"] = float('inf')

        # 抖动计算仍然需要最近100个点
        cycles_list = list(stats["轮询周期记录"])
        recent_cycles = cycles_list[-100:] if len(cycles_list) > 100 else cycles_list

        if not recent_cycles:
            cycle_stats["轮询周期抖动"] = 0.0
            return

        # 计算抖动（标准差）
        if len(recent_cycles) > 1:
            # 使用Welford算法提高数值稳定性
            mean = 0.0
            m2 = 0.0
            for i, x in enumerate(recent_cycles, 1):
                delta = x - mean
                mean += delta / i
                delta2 = x - mean
                m2 += delta * delta2

            cycle_stats["轮询周期抖动"] = (m2 / (len(recent_cycles) - 1)) ** 0.5
        else:
            cycle_stats["轮询周期抖动"] = 0.0

    def _get_client_poll_cycle_stats_str(self, master_id):
        """获取单个客户端的轮询周期统计字符串"""
        # self._update_poll_cycle_stats(master_id)
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
            self._update_poll_cycle_stats(master_id)  # 使用最新数据
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

    async def _client_loop(self, conn, master_id, cycle_time, end_time):
        """基于响应时间的精确周期控制"""
        # 获取该客户端的周期配置
        config = settings.MASTER_CONFIGS.get(master_id, {})
        cycle_time = config.get("cycle_time") or 1.0 / settings.TARGET_FREQUENCY

        # 初始化迭代计数
        iteration_count = 0
        next_start_time = self._clock()  # 下次操作开始时间

        # 让出控制权，确保事件循环启动
        await asyncio.sleep(0)

        while self._clock() < end_time:
            iteration_count += 1

            # 精确等待到目标时间
            current_time = self._clock()
            wait_time = next_start_time - current_time

            if wait_time > 0:
                # 优化等待策略（无异步等待）
                self._precise_wait(next_start_time)

            # 记录操作开始时间
            cycle_start = self._clock()

            # 执行操作
            try:
                await self._random_operation(conn, master_id)
            except Exception as e:
                logger.error(f"操作失败: {e}")

            # 计算实际执行时间
            operation_duration = self._clock() - cycle_start

            # 计算下次开始时间（基于当前完成时间+周期-执行时间）
            next_start_time = max(
                self._clock() + cycle_time - operation_duration,
                self._clock()  # 确保不会倒退
            )

            # 定期打印统计（降低频率）
            if iteration_count % 500 == 0:
                async with self.stats_lock:
                    self.print_all_client_poll_cycle_stats()


    async def run_test(self, duration):
        """多连接并行压力测试"""
        logger.info(f"启动压力测试(客户端数:{len(self.master_ids)})...")
        end_time = self._clock() + duration

        # 为每个客户端创建连接
        connections = {}
        for master_id in self.master_ids:
            try:
                # 获取为该客户端分配的连接ID
                conn_id = self.client_conn_ids[master_id]

                # 使用连接ID获取特定连接
                conn = await self.pool.get_connection(conn_id=conn_id)
                connections[master_id] = conn

                # 获取连接详细信息
                conn_info = self._get_conn_info(conn)
                logger.success(f"客户端 [{master_id}] 连接建立 | {conn_info}")

                # 记录本地端口用于验证
                if hasattr(conn, 'comm_params') and conn.comm_params.source_address:
                    port = conn.comm_params.source_address[1]
                    logger.debug(f"客户端 [{master_id}] 使用本地端口: {port}")
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

    async def _safe_close_connections(self, connections):
        """安全关闭连接集合"""
        if not connections:
            return

        await asyncio.gather(
            *[self._safe_close(c) for c in connections],
            return_exceptions=True
        )

    async def _safe_close(self, client):
        """安全关闭单个连接 - 修复None问题和异步方法检查"""
        if client is None:
            return

        try:
            # 检查是否有关闭方法
            if not hasattr(client, 'close'):
                logger.warning(f"连接对象无close方法: {type(client)}")
                return

            # 检查关闭方法是否是协程
            close_method = client.close
            if asyncio.iscoroutinefunction(close_method):
                await asyncio.wait_for(close_method(), timeout=1.0)
            else:
                # 如果是同步方法，在事件循环中执行
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, close_method)

        except asyncio.TimeoutError:
            logger.warning(f"关闭连接超时: {self._get_conn_info(client)}")
        except Exception as e:
            logger.warning(f"关闭连接异常: {e} | {self._get_conn_info(client)}")

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

        # 3. 安全关闭连接池
        if hasattr(self, 'pool') and self.pool is not None:
            try:
                # 添加超时保护 - 修复await问题
                close_task = self.pool.close_all()
                if asyncio.iscoroutine(close_task):
                    await asyncio.wait_for(close_task, timeout=5.0)
                else:
                    logger.debug("连接池关闭返回非协程对象，直接调用")
                    close_task()

                logger.debug("连接池已关闭")
            except asyncio.TimeoutError:
                logger.error("关闭连接池超时")
                cleanup_errors += 1
            except Exception as e:
                logger.error(f"关闭连接池失败: {type(e).__name__} - {e}")
                cleanup_errors += 1
        else:
            logger.debug("连接池不存在或已关闭，跳过关闭操作")

        if cleanup_errors > 0:
            logger.warning(f"清理完成，但有{cleanup_errors}个错误")
        else:
            logger.info("所有资源已安全释放")