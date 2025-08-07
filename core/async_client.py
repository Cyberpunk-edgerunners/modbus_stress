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
from pathlib import Path

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

        # 3. 关闭连接池（带超时保护）
        if hasattr(self, 'pool'):
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



