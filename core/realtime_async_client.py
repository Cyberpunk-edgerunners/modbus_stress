import sys
import os
import time
import ctypes
import random
import asyncio
import json
import queue
import threading
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
        self._init_cpu_wait()
        self.client_stats = {}
        self.stats_lock = asyncio.Lock()

        # 重构时间点记录系统 - 每个客户端独立上下文
        self.client_context = {
            master_id: {
                "last_network_request_sent": None,  # 精确网络请求发送时间
                "last_network_response_received": None,  # 精确网络响应接收时间
                "operation_lock": asyncio.Lock()  # 每个客户端独立的锁
            } for master_id in master_ids
        }

        # 为每个客户端分配连接ID
        self.client_conn_ids = {
            master_id: idx for idx, master_id in enumerate(master_ids)
        }

        # 为每个客户端初始化统计
        for master_id in master_ids:
            self._init_client_stats(master_id)

        # 初始化全局统计
        self.global_stats = {
            "start_time": self._clock(),
            "total_requests": 0,
            "success_requests": 0,
            "failed_requests": 0,
            "timeout_requests": 0,
            "轮询周期记录": deque(maxlen=10000),  # 全局记录，最大10000条
            "轮询周期统计": {
                "最大值": 0.0,
                "最小值": float('inf'),
                "p50": 0.0,
                "p95": 0.0,
                "p99": 0.0
            }
        }

        # 优化报文捕获系统 - 使用后台线程
        self.packet_capture_enabled = True
        self.packet_queue = queue.Queue()
        self.log_writer_thread = None
        self.capture_start_time = None
        self.capture_end_time = None
        self.logs_dir = Path("logs")
        self.logs_dir.mkdir(exist_ok=True)

        # 批处理变量
        self._current_batch = []
        self._current_filename = None

        # 启动日志写入线程
        self._start_log_writer()

        # 添加系统时间基准
        self.system_start_time = time.time()
        self.qpc_start_time = self._clock()

    def _init_client_stats(self, master_id):
        """为每个客户端初始化统计信息"""
        self.client_stats[master_id] = {
            "总请求数": 0,
            "成功请求": 0,
            "失败请求": 0,
            "超时请求": 0,
            "开始时间": self._clock(),
            "轮询周期记录": deque(maxlen=1000),
            "等待时间记录": deque(maxlen=1000),
            "报文延迟记录": deque(maxlen=1000),
            "轮询周期统计": {
                "平均轮询周期": 0.0,
                "最大轮询周期": 0.0,
                "最小轮询周期": float('inf'),
                "轮询周期抖动": 0.0,
                "最大轮询周期抖动": 0.0
            },
            "轮询周期累积": {
                "总和": 0.0,
                "计数": 0,
                "最大值": 0.0,
                "最小值": float('inf')
            },
            "等待时间统计": {
                "平均等待时间": 0.0,
                "最大等待时间": 0.0,
                "最小等待时间": float('inf'),
                "等待时间抖动": 0.0,
                "最大等待时间抖动": 0.0
            },
            "等待时间累积": {
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

    # 修改时钟获取函数，直接使用最高精度接口
    def _init_clock(self):
        """高精度时钟源（直接使用原生API不缓存）"""
        if sys.platform == "win32":
            self._kernel32 = ctypes.windll.kernel32
            self._qpc_freq = ctypes.c_int64()
            self._kernel32.QueryPerformanceFrequency(ctypes.byref(self._qpc_freq))
            self._clock = self._qpc_counter  # 直接使用QPC
        else:
            self._clock = time.perf_counter  # Linux使用perf_counter

    def _qpc_counter(self):
        """Windows QPC时钟实现"""
        counter = ctypes.c_int64()
        self._kernel32.QueryPerformanceCounter(ctypes.byref(counter))
        return counter.value / self._qpc_freq.value

    # def _cached_perf_counter(self):
    #     """带缓存的perf_counter实现"""
    #     current = time.perf_counter()
    #     # 每毫秒更新一次缓存
    #     if int(current * 1000) != int(self._last_time * 1000):
    #         self._last_time = current
    #     return self._last_time

    #优化缓存时钟改为实时获取
    # def _cached_qpc_counter(self):
    #     """带缓存的QPC实现"""
    #     counter = ctypes.c_int64()
    #     self._kernel32.QueryPerformanceCounter(ctypes.byref(counter))
    #     current = counter.value / self._qpc_freq.value
    #     # 每毫秒更新一次缓存
    #     if int(current * 1000) != int(self._last_time * 1000):
    #         self._last_time = current
    #     return self._last_time


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

    def _init_cpu_wait(self):
        """初始化CPU等待函数"""
        self._cpu_wait_fn = None

        # 尝试加载YieldProcessor
        if sys.platform == "win32":
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            try:
                # 检查YieldProcessor是否可用
                kernel32.YieldProcessor
                self._cpu_wait_fn = lambda: kernel32.YieldProcessor()
                logger.debug("使用YieldProcessor进行等待优化")
            except AttributeError:
                logger.warning("YieldProcessor不可用，使用空指令替代")

        # 如果YieldProcessor不可用，使用空函数替代
        if self._cpu_wait_fn is None:
            # 使用空操作指令作为替代
            self._cpu_wait_fn = lambda: None

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

    def _get_real_timestamp(self, qpc_time):
        """将QPC时间转换为Unix时间戳"""
        elapsed = qpc_time - self.qpc_start_time
        return self.system_start_time + elapsed

    def _start_log_writer(self):
        """启动后台日志写入线程"""
        if self.log_writer_thread is None or not self.log_writer_thread.is_alive():
            self.log_writer_thread = threading.Thread(
                target=self._log_writer_worker,
                daemon=True,
                name="PacketLogger"
            )
            self.log_writer_thread.start()
            logger.info("报文记录线程已启动")

    def _log_writer_worker(self):
        """报文写入工作线程"""
        while True:
            try:
                filename, packets = self.packet_queue.get(timeout=1.0)

                try:
                    with open(filename, "a", encoding=settings.CAPTURE_FILE_ENCODING) as f:
                        for packet in packets:
                            # 使用预转换的时间戳字符串
                            log_line = f"{packet['timestamp_str']} | {packet['type']} | Master: {packet['master_id']}"

                            # 添加端口信息
                            if 'src_port' in packet and 'dst_port' in packet:
                                log_line += f" | Src: {packet['src_port']} | Dst: {packet['dst_port']}"

                            # 添加操作信息（如果存在）
                            if packet['type'] in ('request', 'response'):
                                log_line += f" | Op: {packet.get('operation', '?')}"
                                log_line += f" | Addr: {packet.get('address', '?')}"
                                log_line += f" | Count: {packet.get('count', '?')}"

                            # 添加额外字段
                            if packet['type'] == 'response':
                                log_line += f" | Latency: {packet.get('latency_ms', '?')}ms"
                            elif packet['type'] == 'error':
                                log_line += f" | Error: {packet.get('error_type', '?')} - {packet.get('error_msg', '?')}"

                            f.write(log_line + "\n")

                    # logger.debug(f"已写入 {len(packets)} 条报文到 {filename}")
                except Exception as e:
                    logger.error(f"写入报文失败: {e}")

                # 标记任务完成
                self.packet_queue.task_done()
            except queue.Empty:
                # 检查是否测试结束
                if self.capture_end_time and self._clock() > self.capture_end_time + 10:
                    break
            except Exception as e:
                logger.error(f"报文记录线程错误: {e}")
                time.sleep(0.1)

    async def _log_packet(self, packet_data):
        try:
            # 确保每次都有有效的文件名
            real_timestamp = self._get_real_timestamp(packet_data['timestamp'])
            date_str = datetime.fromtimestamp(real_timestamp).strftime("%Y%m%d")
            filename = self.logs_dir / f"modbus_packets_{date_str}.log"

            # 确保当前文件名始终有效
            if not hasattr(self, '_current_filename') or self._current_filename is None:
                self._current_filename = filename

            # 当批次达到阈值或文件名变更时提交
            if len(self._current_batch) >= 100 or filename != self._current_filename:
                self.packet_queue.put((self._current_filename, self._current_batch))
                self._current_batch = []
                self._current_filename = filename

            # 添加到当前批次
            self._current_batch.append({
                **packet_data,
                "timestamp_str": datetime.fromtimestamp(real_timestamp).strftime('%Y-%m-%d %H:%M:%S.%f')
            })
        except Exception as e:
            logger.error(f"提交报文失败: {e}")
    # 暂时不用
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

    # 暂时不用
    def _busy_wait(self, duration):
        """优化的忙等待"""
        end_time = self._clock() + duration
        while self._clock() < end_time:
            if end_time - self._clock() > 0.001:  # >1ms剩余时短暂释放
                time.sleep(0.0001)  # 100μs级释放

    # 暂时不用
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
        """优化的忙等待（兼容所有平台）"""
        if duration <= 0:
            return

        end_time = self._clock() + duration
        check_counter = 0

        while self._clock() < end_time:
            # 周期性地使用CPU等待函数
            if check_counter % 100 == 0:
                self._cpu_wait_fn()

            check_counter += 1

            # 长时间等待时偶尔让出控制权
            if duration > 0.001 and check_counter % 1000 == 0:
                time.sleep(0)  # 让出控制权但立即返回

    # 暂时不用
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

    # 暂时不用
    def _micro_spin_wait(self, duration):
        """微秒级精度的忙等待"""
        if duration <= 0:
            return

        end_time = self._clock() + duration
        while self._clock() < end_time:
            pass

    def _precision_wait_until(self, target_time):
        """精确等待直到目标时间（使用优化的忙等待）"""
        current_time = self._clock()

        # 初始快速旋转（适用于极短等待）
        while current_time < target_time and (target_time - current_time) < 0.0001:  # < 100μs
            self._cpu_wait_fn()
            current_time = self._clock()

        # 分级等待策略（确保最大兼容性）
        while current_time < target_time:
            remaining = target_time - current_time

            # 大于1ms时使用混合等待
            if remaining > 0.001:  # >1ms
                sleep_time = remaining * 0.7
                spin_time = remaining - sleep_time
                time.sleep(sleep_time)
                self._spin_wait(spin_time)
                return

            # 100μs-1ms使用纯忙等待
            elif remaining > 0.0001:  # 100μs-1ms
                self._spin_wait(remaining)
                return

            # 小于100μs使用空指令
            else:
                self._cpu_wait_fn()
                current_time = self._clock()

    async def _log_request(self, master_id, send_time, op_type, addr, count, values,
                           client_port, server_port):
        """记录请求报文"""
        await self._log_packet({
            "timestamp": send_time,
            "type": "request",
            "master_id": master_id,
            "src_port": client_port,  # 源端口=客户端
            "dst_port": server_port,  # 目的端口=服务器
            "operation": op_type,
            "address": addr,
            "count": count,
            "values": values
        })

    async def _log_response(self, master_id, recv_time, op_type, addr, count,
                            network_latency_ms, client_port, server_port):
        """记录响应报文"""
        await self._log_packet({
            "timestamp": recv_time,
            "type": "response",
            "master_id": master_id,
            "src_port": server_port,  # 源端口=服务器
            "dst_port": client_port,  # 目的端口=客户端
            "operation": op_type,  # 添加操作信息
            "address": addr,  # 添加地址
            "count": count,  # 添加数量
            "latency_ms": network_latency_ms
        })

    async def _log_error(self, master_id, recv_time, op_type, addr, count,
                         error_type, error_msg, client_port, server_port):
        """记录错误报文"""
        await self._log_packet({
            "timestamp": recv_time,
            "type": "error",
            "master_id": master_id,
            "src_port": client_port,  # 源端口=客户端
            "dst_port": server_port,  # 目的端口=服务器
            "operation": op_type,  # 添加操作信息
            "address": addr,  # 添加地址
            "count": count,  # 添加数量
            "error_type": error_type,
            "error_msg": error_msg
        })

    async def _update_timing_stats(self, master_id, send_time,
                                   last_send_time, last_response_time,
                                   network_latency_ms, op_type):
        """精确更新时序统计信息"""
        # 1. 计算真实轮询周期（两次请求发送间隔）
        true_poll_interval = (send_time - last_send_time) * 1000

        # 2. 计算等待时间（本次请求发送 - 上次响应接收）
        wait_time = (send_time - last_response_time) * 1000

        # 更新全局轮询周期记录
        self.global_stats["轮询周期记录"].append(true_poll_interval)

        # 更新全局最大值和最小值
        if true_poll_interval > self.global_stats["轮询周期统计"]["最大值"]:
            self.global_stats["轮询周期统计"]["最大值"] = true_poll_interval
        if true_poll_interval < self.global_stats["轮询周期统计"]["最小值"]:
            self.global_stats["轮询周期统计"]["最小值"] = true_poll_interval

        # 更新客户端统计
        client_stats = self.client_stats[master_id]

        # 轮询周期统计
        poll_cum_stats = client_stats["轮询周期累积"]
        poll_cum_stats["总和"] += true_poll_interval
        poll_cum_stats["计数"] += 1
        if true_poll_interval > poll_cum_stats["最大值"]:
            poll_cum_stats["最大值"] = true_poll_interval
        if true_poll_interval < poll_cum_stats["最小值"]:
            poll_cum_stats["最小值"] = true_poll_interval

        client_stats["轮询周期记录"].append(true_poll_interval)

        # 等待时间统计
        wait_cum_stats = client_stats["等待时间累积"]
        wait_cum_stats["总和"] += wait_time
        wait_cum_stats["计数"] += 1
        if wait_time > wait_cum_stats["最大值"]:
            wait_cum_stats["最大值"] = wait_time
        if wait_time < wait_cum_stats["最小值"]:
            wait_cum_stats["最小值"] = wait_time

        client_stats["等待时间记录"].append(wait_time)

        # 更新延迟统计
        async with self.stats_lock:
            latency_key = {
                0: "read_input_registers",
                1: "read_holding_registers",
                2: "write_registers"
            }[op_type]

            stats = self.client_stats[master_id]
            stats["报文延迟统计"][latency_key].append(network_latency_ms)
            stats["报文延迟统计"]["所有报文"].append(network_latency_ms)
            stats["成功请求"] += 1
            stats["总请求数"] += 1
            self.global_stats["total_requests"] += 1
            self.global_stats["success_requests"] += 1

    async def _random_operation(self, client, master_id):
        """执行随机Modbus操作（精确统计）"""
        # 获取客户端上下文
        ctx = self.client_context[master_id]

        # 随机选择操作类型
        op_type = random.randint(0, 2)

        # 根据操作类型设置寄存器范围和计数
        if op_type == 0:  # 读输入寄存器
            addr_range = settings.INPUT_REGISTER_RANGE
            count = min(random.randint(1, 10), addr_range[1] - addr_range[0] + 1)
            addr = random.randint(addr_range[0], addr_range[1] - count + 1)
            register_type = "input"
        else:  # 读保持寄存器或写保持寄存器
            addr_range = settings.HOLDING_REGISTER_RANGE
            if op_type == 1:  # 读保持寄存器
                count = settings.MAX_REGISTERS_PER_READ
            else:  # 写保持寄存器
                count = settings.MAX_REGISTERS_PER_WRITE
            # 确保起始地址不越界
            max_addr = addr_range[1] - count + 1
            addr = random.randint(addr_range[0], max_addr)
            register_type = "holding"

        values = None
        # logger.debug(
        #     f"客户端 [{master_id}] 操作: {op_type} | 寄存器类型: {register_type} | 起始地址: {addr} | 数量: {count}")

        # 获取端口信息（网络视角）
        if hasattr(client.comm_params, 'source_address'):
            client_port = client.comm_params.source_address[1]
            server_port = client.comm_params.port
        else:
            client_port = 0
            server_port = settings.CONTROLLER_PORT

        # 准备写入值（仅写操作）
        if op_type == 2:  # Write操作
            values = [random.randint(0, 65535) for _ in range(count)]

        try:
            # 使用连接池执行请求并获取精确时间戳
            result, send_time, recv_time, active_client = await self.pool.execute(
                client,
                op_type,
                addr,
                count,
                values
            )

            # 确保使用返回的客户端对象（可能在异常修复后重新创建）
            client = active_client

            # 计算网络延迟
            network_latency_ms = (recv_time - send_time) * 1000

            # 更新网络层时间戳
            async with ctx["operation_lock"]:
                # 保存旧的时间戳
                last_send_time = ctx["last_network_request_sent"]
                last_response_time = ctx["last_network_response_received"]

                # 更新新的时间戳
                ctx["last_network_request_sent"] = send_time
                ctx["last_network_response_received"] = recv_time

                # 更新时序统计（只在有历史数据时）
                if last_send_time is not None and last_response_time is not None:
                    await self._update_timing_stats(
                        master_id,
                        send_time,
                        last_send_time,
                        last_response_time,
                        network_latency_ms,
                        op_type
                    )
                else:
                    # 首次请求只更新基础统计
                    async with self.stats_lock:
                        stats = self.client_stats[master_id]
                        stats["成功请求"] += 1
                        stats["总请求数"] += 1
                        self.global_stats["total_requests"] += 1
                        self.global_stats["success_requests"] += 1

            # 记录请求和响应报文
            await self._log_request(
                master_id, send_time, op_type, addr, count, values, client_port, server_port
            )
            await self._log_response(
                master_id, recv_time, op_type, addr, count, network_latency_ms, client_port, server_port
            )

            return True

        except asyncio.TimeoutError:
            # 记录超时请求
            async with self.stats_lock:
                self.client_stats[master_id]["超时请求"] += 1
                self.global_stats["timeout_requests"] += 1
                self.client_stats[master_id]["总请求数"] += 1
            logger.error(f"客户端 [{master_id}] 操作超时 | 操作: {op_type} | 地址: {addr} | 数量: {count}")

            # 记录错误报文
            recv_time = self._clock()  # 使用当前时间作为错误发生时间
            await self._log_error(
                master_id, recv_time, op_type, addr, count,
                "TimeoutError", "操作超时", client_port, server_port
            )
            return False

        except ModbusException as e:
            logger.error(f"Modbus操作失败: {e} | 操作: {op_type} | 地址: {addr} | 数量: {count}")
            async with self.stats_lock:
                self.client_stats[master_id]["失败请求"] += 1
                self.client_stats[master_id]["总请求数"] += 1
                self.global_stats["total_requests"] += 1
                self.global_stats["failed_requests"] += 1

            # 记录错误报文
            recv_time = self._clock()
            await self._log_error(
                master_id, recv_time, op_type, addr, count,
                "ModbusException", str(e), client_port, server_port
            )
            return False

        except ConnectionException as e:
            logger.error(f"连接异常: {e} | 操作: {op_type} | 地址: {addr} | 数量: {count}")
            async with self.stats_lock:
                self.client_stats[master_id]["失败请求"] += 1
                self.client_stats[master_id]["总请求数"] += 1
                self.global_stats["total_requests"] += 1
                self.global_stats["failed_requests"] += 1

            # 记录错误报文
            recv_time = self._clock()
            await self._log_error(
                master_id, recv_time, op_type, addr, count,
                "ConnectionException", str(e), client_port, server_port
            )
            return False

        except Exception as e:
            logger.error(f"操作异常: {e} | 操作: {op_type} | 地址: {addr} | 数量: {count}")
            async with self.stats_lock:
                self.client_stats[master_id]["失败请求"] += 1
                self.client_stats[master_id]["总请求数"] += 1
                self.global_stats["total_requests"] += 1
                self.global_stats["failed_requests"] += 1

            # 记录错误报文
            recv_time = self._clock()
            await self._log_error(
                master_id, recv_time, op_type, addr, count,
                type(e).__name__, str(e), client_port, server_port
            )
            return False

    def _calculate_global_percentiles(self):
        """计算全局轮询周期百分位"""
        cycles = list(self.global_stats["轮询周期记录"])
        if not cycles:
            return

        sorted_cycles = sorted(cycles)
        n = len(sorted_cycles)

        self.global_stats["轮询周期统计"]["p50"] = sorted_cycles[int(n * 0.50)]
        self.global_stats["轮询周期统计"]["p95"] = sorted_cycles[int(n * 0.95)]
        self.global_stats["轮询周期统计"]["p99"] = sorted_cycles[int(n * 0.99)]

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

    def _update_poll_cycle_stats(self, master_id):
        """更新轮询周期统计（使用累积变量）"""
        stats = self.client_stats[master_id]
        cum_stats = stats["轮询周期累积"]
        cycle_stats = stats["轮询周期统计"]

        # 使用累积变量计算平均值
        if cum_stats["计数"] > 0:
            cycle_stats["平均轮询周期"] = cum_stats["总和"] / cum_stats["计数"]
            cycle_stats["最大轮询极期"] = cum_stats["最大值"]
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

            current_jitter = (m2 / (len(recent_cycles) - 1)) ** 0.5
            cycle_stats["轮询周期抖动"] = current_jitter
            if current_jitter > cycle_stats["最大轮询周期抖动"]:
                cycle_stats["最大轮询周期抖动"] = current_jitter
        else:
            cycle_stats["轮询周期抖动"] = 0.0

    def _update_wait_time_stats(self, master_id):
        """高效更新等待时间统计"""
        stats = self.client_stats[master_id]
        wait_stats = stats["等待时间统计"]
        cum_stats = stats["等待时间累积"]
        wait_records = stats["等待时间记录"]

        # 使用累积变量计算平均值
        if cum_stats["计数"] > 0:
            wait_stats["平均等待时间"] = cum_stats["总和"] / cum_stats["计数"]
            wait_stats["最大等待时间"] = cum_stats["最大值"]
            wait_stats["最小等待时间"] = cum_stats["最小值"]
        else:
            wait_stats["平均等待时间"] = 0.0
            wait_stats["最大等待时间"] = 0.0
            wait_stats["最小等待时间"] = float('inf')

        # 抖动计算使用最近100个点
        recent_waits = list(wait_records)[-100:] if len(wait_records) > 100 else list(wait_records)

        if not recent_waits:
            wait_stats["等待时间抖动"] = 0.0
            return

        # 计算抖动（标准差）
        if len(recent_waits) > 1:
            # 使用Welford算法提高数值稳定性
            mean = 0.0
            m2 = 0.0
            for i, x in enumerate(recent_waits, 1):
                delta = x - mean
                mean += delta / i
                delta2 = x - mean
                m2 += delta * delta2

            current_jitter = (m2 / (len(recent_waits) - 1)) ** 0.5
            wait_stats["等待时间抖动"] = current_jitter
            if current_jitter > wait_stats["最大等待时间抖动"]:
                wait_stats["最大等待时间抖动"] = current_jitter
        else:
            wait_stats["等待时间抖动"] = 0.0

    def _get_client_poll_cycle_stats_str(self, master_id):
        """重构客户端统计字符串 - 显示等待时间和抖动"""
        poll_stats = self.client_stats[master_id]["轮询周期统计"]
        wait_stats = self.client_stats[master_id]["等待时间统计"]

        # 格式化数字
        formatted_poll_avg = f"{poll_stats['平均轮询周期']:.3f}".rjust(6)
        formatted_wait_avg = f"{wait_stats['平均等待时间']:.3f}".rjust(6)
        formatted_wait_jitter = f"{wait_stats['等待时间抖动']:.3f}".rjust(6)
        formatted_max_wait_jitter = f"{wait_stats['最大等待时间抖动']:.3f}".rjust(6)

        return (
            f"[客户端 {master_id}] "
            f"轮询周期: {formatted_poll_avg}ms | "
            f"等待时间: {formatted_wait_avg}ms | "
            f"抖动: {formatted_wait_jitter}ms | "
            f"最大抖动: {formatted_max_wait_jitter}ms"
        )

    def print_all_client_poll_cycle_stats(self):
        """打印所有客户端的轮询周期统计信息 - 更新等待时间统计"""
        # 清屏或移动光标到行首
        print("\033[F" * (len(self.master_ids) + 1), end="")  # 移动光标到上一行

        # 打印所有客户端统计
        for master_id in self.master_ids:
            self._update_poll_cycle_stats(master_id)  # 轮询周期统计
            self._update_wait_time_stats(master_id)  # 新增：更新等待时间统计
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
        """基于等待时间的精确周期控制"""
        ctx = self.client_context[master_id]
        iteration_count = 0
        last_operation_end = self._clock()  # 跟踪上次操作结束时间

        while self._clock() < end_time:
            iteration_count += 1

            # 计算下一次发送时间（基于设定的等待时间）
            next_start_time = last_operation_end + cycle_time

            # 精确等待到发送时间
            current_time = self._clock()
            if current_time < next_start_time:
                self._precision_wait_until(next_start_time)

            try:
                # 执行操作并记录开始时间
                operation_start = self._clock()
                await self._random_operation(conn, master_id)
            except Exception as e:
                logger.error(f"操作失败: {e}")
            finally:
                # 记录操作结束时间
                last_operation_end = self._clock()

            # 定期打印统计（减少频率）
            if iteration_count % 500 == 0:
                async with self.stats_lock:
                    self.print_all_client_poll_cycle_stats()

                # 每500次操作检查系统时钟精度
                if sys.platform == "win32" and iteration_count % 10000 == 0:
                    self._check_clock_precision()


    def _check_clock_precision(self):
        """检查并维护系统时钟精度"""
        if not hasattr(self, '_winmm'):
            return

        try:
            # 每隔一段时间重新设置时钟精度
            self._winmm.timeEndPeriod(1)
            self._winmm.timeBeginPeriod(1)
            # logger.debug("系统时钟精度已刷新")
        except Exception as e:
            logger.warning(f"刷新时钟精度失败: {e}")

    async def run_test(self, duration):
        """多连接并行压力测试"""
        # 初始化抓包时间窗口
        self.capture_start_time = self._clock()
        self.capture_end_time = self.capture_start_time + settings.PACKET_CAPTURE_DURATION

        logger.info(f"启动压力测试(客户端数:{len(self.master_ids)}), 报文捕获将持续 {settings.PACKET_CAPTURE_DURATION} 秒...")
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
        self._calculate_global_percentiles()
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

        # 添加全局轮询周期统计
        global_cycle_stats = self.global_stats["轮询周期统计"]

        report_lines.extend([
            f"运行时长: {duration:.2f}秒",
            f"总请求数: {total_requests}",
            f"成功请求: {total_success}",
            f"失败请求: {total_failures}",
            f"超时请求: {total_timeouts}",
            f"QPS: {qps:.2f}",
            f"成功率: {success_rate:.2f}%",
            "",
            "全局轮询周期统计:",
            f"  最小轮询周期: {global_cycle_stats['最小值']:.3f}ms",
            f"  最大轮询周期: {global_cycle_stats['最大值']:.3f}ms",
            f"  P50: {global_cycle_stats['p50']:.3f}ms",
            f"  P95: {global_cycle_stats['p95']:.3f}ms",
            f"  P99: {global_cycle_stats['p99']:.3f}ms",
        ""
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
                f"  最大抖动: {stats['轮询周期统计']['最大轮询周期抖动']:.6f}ms",
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

        # 1. 提交剩余的报文批次
        if hasattr(self, '_current_batch') and self._current_batch:
            try:
                self.packet_queue.put((self._current_filename, self._current_batch))
                self._current_batch = []
                logger.debug("已提交剩余报文批次")
            except Exception as e:
                logger.error(f"提交剩余报文失败: {e}")
                cleanup_errors += 1

        # 2. 等待队列处理完成
        if self.log_writer_thread and self.log_writer_thread.is_alive():
            try:
                # 等待最多3秒
                logger.info("等待报文记录线程完成...")
                self.log_writer_thread.join(3.0)
                if self.log_writer_thread.is_alive():
                    logger.warning("报文记录线程仍在运行，强制结束")
                    cleanup_errors += 1
                else:
                    logger.info("报文记录线程已安全退出")
            except Exception as e:
                logger.error(f"等待报文记录线程结束失败: {e}")
                cleanup_errors += 1

        # 3. 确保测试报告生成
        try:
            self._generate_report()
        except Exception as e:
            logger.error(f"生成报告失败: {type(e).__name__} - {e}")
            cleanup_errors += 1

        # 4. 恢复时钟精度
        if hasattr(self, '_winmm'):
            try:
                self._winmm.timeEndPeriod(1)
                logger.debug("系统时钟精度已恢复")
            except Exception as e:
                logger.error(f"恢复时钟精度失败: {type(e).__name__} - {e}")
                cleanup_errors += 1

        # 5. 安全关闭连接池
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