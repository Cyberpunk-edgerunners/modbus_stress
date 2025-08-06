import sys
import time
import ctypes
import random
import asyncio
from datetime import datetime
from loguru import logger
from pymodbus.exceptions import ModbusException
from .async_connection import AsyncModbusConnection
from config import settings

class HighPrecisionAsyncModbusClient:
    """高精度异步Modbus客户端"""
    def __init__(self):
        self.pool = AsyncModbusConnection()
        self._init_clock()
        self._set_clock_resolution()
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

    def _set_clock_resolution(self):
        """Windows平台设置高精度时钟分辨率"""
        if sys.platform == 'win32':
            self._winmm = ctypes.windll.winmm
            self._winmm.timeBeginPeriod(1)

    def _busy_wait(self, target_delay):
        """高精度忙等待"""
        start = self._clock()
        while True:
            current = self._clock()
            if current - start >= target_delay:
                break
            if target_delay - (current - start) > 0.002:
                time.sleep(0.001)

    async def _random_operation(self, client):
        """执行随机Modbus操作（修正版）"""
        op_type = random.randint(0, 2)
        addr = random.randint(*settings.HOLDING_REGISTER_RANGE)
        count = min(random.randint(1, 10), settings.MAX_REGISTERS_PER_READ)

        try:
            start = self._clock()
            if op_type == 0:
                await client.read_input_registers(address=addr, count=count)
            elif op_type == 1:
                await client.read_holding_registers(address=addr, count=count)
            else:
                values = [random.randint(0, 65535) for _ in range(count)]
                await client.write_registers(address=addr, values=values)

            latency = (self._clock() - start) * 1000
            self.stats["延迟记录"].append(latency)
            self.stats["成功请求"] += 1
            return True
        except ModbusException as e:
            logger.error(f"Modbus操作失败: {e}")
            self.stats["失败请求"] += 1
            return False
        finally:
            self.stats["总请求数"] += 1

    def _update_cycle_stats(self, cycle_time):
        """更新周期统计数据"""
        cycle_ms = cycle_time * 1000
        self.stats["周期记录"].append(cycle_ms)

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

    async def run_test(self, duration):
        """运行异步压力测试"""
        logger.info("开始异步长连接压力测试...")
        end_time = self._clock() + duration
        client = await self.pool.get_connection()

        # 预热阶段(忽略前10个周期的统计)
        warmup_cycles = 10

        while self._clock() < end_time:
            cycle_start = self._clock()

            try:
                # 减少并发数以提高稳定性
                tasks = [self._random_operation(client) for _ in range(3)]  # 从5降到3
                await asyncio.gather(*tasks)
            except Exception as e:
                logger.error(f"测试异常: {e}")
                client = await self.pool.get_connection()

            # 精确周期控制(动态调整)
            elapsed = self._clock() - cycle_start
            target_wait = max(0, settings.BUSY_WAIT_PRECISION - elapsed)
            if target_wait > 0.001:  # 只对较长的等待使用sleep
                await asyncio.sleep(target_wait * 0.5)  # 部分异步等待
                self._busy_wait(target_wait * 0.5)  # 部分忙等待

            # 更新统计(跳过预热周期)
            if warmup_cycles <= 0:
                self._update_cycle_stats(self._clock() - cycle_start)
            else:
                warmup_cycles -= 1

        self._generate_report()

    def _generate_report(self):
        """生成中文测试报告"""
        duration = self._clock() - self.stats["开始时间"]
        qps = self.stats["总请求数"] / duration
        success_rate = (self.stats["成功请求"] / self.stats["总请求数"]) * 100

        report = f"""
=== Modbus异步测试报告 ===
测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
运行时长: {duration:.2f}秒
总请求数: {self.stats["总请求数"]}
成功请求: {self.stats["成功请求"]}
失败请求: {self.stats["失败请求"]}
QPS: {qps:.2f}
成功率: {success_rate:.2f}%
--- 周期统计 ---
平均周期: {self.stats['周期统计']['平均周期']:.6f}ms
最大周期: {self.stats['周期统计']['最大周期']:.6f}ms
最小周期: {self.stats['周期统计']['最小周期']:.6f}ms
周期抖动: {self.stats['周期统计']['周期抖动']:.6f}ms
"""
        print(report)

    async def cleanup(self):
        """异步清理资源"""
        if hasattr(self, '_winmm'):
            self._winmm.timeEndPeriod(1)
        await self.pool.close_all()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.cleanup()


