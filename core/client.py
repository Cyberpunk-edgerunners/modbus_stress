import time
import ctypes
import random
import socket
from datetime import datetime
from loguru import logger
from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException
from config import settings
from .connection import ModbusConnectionPool


class HighPrecisionModbusClient:
    def __init__(self):
        self.pool = ModbusConnectionPool()
        self.stats = {
            "总请求数": 0,
            "成功请求": 0,
            "失败请求": 0,
            "开始时间": time.time(),
            "延迟记录": [],
            "周期记录": [],
            "周期统计": {
                "平均周期": 0.0,
                "最大周期": 0.0,
                "最小周期": float('inf'),
                "周期抖动": 0.0
            }
        }

    def _setup_client(self):
        """配置客户端 (兼容PyModbus 3.x)"""
        self.client = ModbusTcpClient(
            host=self.host,
            port=self.port,
            timeout=1.0,
            no_delay=settings.DISABLE_NAGLE  # 禁用Nagle的新方式
        )

        if not self.client.connect():
            logger.error("初始连接失败")
            raise ConnectionError("无法建立初始连接")


    #     self.kernel132 = ctypes.WinDLL('kernel132')
    #     self.frequency = ctypes.c_int64
    #     self.kernel132.QueryPerformanceFrequency(ctypes.byref(self.frequency))

    # def _busy_wait_1ms(self):
    #     """严格执行1ms忙等待，返回实际耗时"""
    #     start = ctypes.c_int64()
    #     self.kernel132.QueryPerformanceFrequency(ctypes.byref(start))
    #
    #     target = start.value + (self.frequency.value // 1000) #目标计数值 (+1ms)
    #
    #     while True:
    #         now = ctypes.c_int64()
    #         self.kernel132.QueryPerformanceFrequency(ctypes.byref(now))
    #         if now.value > target:
    #             actual_ns = (now.value - start.value) *1_000_000_000 // self.frequency.value
    #             return actual_ns / 1_000_000 #返回实际毫秒数

    def _busy_wait(self, target_delay):
        """高精度忙等待"""
        start = time.perf_counter()
        while time.perf_counter() - start < target_delay:
            pass

    def _random_operation(self, conn):
        """执行随机Modbus操作"""
        op_type = random.randint(0, 2)
        addr = random.randint(*settings.HOLDING_REGISTER_RANGE)
        count = random.randint(1, settings.MAX_REGISTERS_PER_READ)

        try:
            start_time = time.time()

            if op_type == 0:  # 读输入寄存器
                result = conn.read_input_registers(address=addr, count=count)
                logger.debug(f"读输入寄存器 {addr}-{addr + count}: {result.registers}")
            elif op_type == 1:  # 读保持寄存器
                result = conn.read_holding_registers(address=addr, count=count)
                logger.debug(f"读保持寄存器 {addr}-{addr + count}: {result.registers}")
            else:  # 写保持寄存器
                values = [random.randint(0, 65535) for _ in range(count)]
                result = conn.write_registers(address=addr, values=values)
                logger.debug(f"写保持寄存器 {addr}-{addr + count}: {values}")

            latency = (time.time() - start_time) * 1000  # 毫秒
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
        # 记录当前周期时间(毫秒)
        self.stats["周期记录"].append(cycle_time * 1000)

        # 更新统计指标
        cycles = self.stats["周期记录"]
        self.stats["周期统计"]["平均周期"] = sum(cycles) / len(cycles)
        self.stats["周期统计"]["最大周期"] = max(cycles)
        self.stats["周期统计"]["最小周期"] = min(cycles)

        # 计算周期抖动(最近10个周期的标准差)
        recent_cycles = cycles[-10:] if len(cycles) >= 10 else cycles
        if len(recent_cycles) > 1:
            mean = sum(recent_cycles) / len(recent_cycles)
            variance = sum((x - mean) ** 2 for x in recent_cycles) / (len(recent_cycles) - 1)
            self.stats["周期统计"]["周期抖动"] = variance ** 0.5

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

    def run_test(self, duration, use_busy_wait=True):
        """运行压力测试"""
        logger.info("开始Modbus压力测试...")
        end_time = time.time() + duration

        while time.time() < end_time:
            cycle_start = time.time()
            conn = self.pool.get_connection()

            try:
                if conn and not self._random_operation(conn):
                    self._handle_connection_error(conn)
            except Exception as e:
                logger.error(f"测试发生异常: {e}")
                self._save_error_report(e)
            finally:
                if conn:
                    self.pool.release_connection(conn)

            # # 精确周期控制
            # if use_busy_wait:
            #     elapsed = time.time() - cycle_start
            #     remaining = max(0, settings.BUSY_WAIT_PRECISION - elapsed)
            #     self._busy_wait(remaining)
            # elif settings.MASTER_CONFIGS.get("cycle_time"):
            #     time.sleep(settings.MASTER_CONFIGS["cycle_time"])

            try:
                if conn and not self._random_operation(conn):
                    self._handle_connection_error(conn)
            except Exception as e:
                logger.error(f"测试发生异常: {e}")
                self._save_error_report(e)
            finally:
                if conn:
                    self.pool.release_connection(conn)

                # 计算并记录周期时间
            cycle_time = time.time() - cycle_start
            self._update_cycle_stats(cycle_time)

            # 实时打印周期统计
            if len(self.stats["周期记录"]) % 100 == 0:  # 每100次打印一次
                self._print_cycle_stats()

        self._generate_report()

    def _handle_connection_error(self, conn):
        """处理连接错误"""
        logger.warning("连接异常，尝试重新连接...")
        conn.close()
        time.sleep(1)
        new_conn = self.pool._create_connection()
        if new_conn:
            self.pool.release_connection(new_conn)

    def _save_error_report(self, error):
        """保存错误报告"""
        error_time = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"logs/error_{error_time}.log"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(f"错误时间: {datetime.now()}\n")
            f.write(f"错误类型: {type(error).__name__}\n")
            f.write(f"错误详情: {str(error)}\n\n")
            f.write("堆栈跟踪:\n")
            import traceback
            traceback.print_exc(file=f)

    def _generate_report(self):
        """生成测试报告"""
        duration = time.time() - self.stats["开始时间"]
        qps = self.stats["总请求数"] / duration
        success_rate = (self.stats["成功请求"] / self.stats["总请求数"]) * 100 if self.stats["总请求数"] > 0 else 0

        latencies = self.stats["延迟记录"]
        avg_latency = sum(latencies) / len(latencies) if latencies else 0
        max_latency = max(latencies) if latencies else 0

        cycle_stats = self.stats["周期统计"]

        report = f"""
=== Modbus压力测试报告 ===
测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
运行时长: {duration:.2f}秒
总请求数: {self.stats["总请求数"]}
成功请求: {self.stats["成功请求"]}
失败请求: {self.stats["失败请求"]}
QPS: {qps:.2f}
成功率: {success_rate:.2f}%
平均延迟: {avg_latency:.2f}ms
最大延迟: {max_latency:.2f}ms
--- 周期统计 ---
平均周期: {cycle_stats['平均周期']:.6f}ms
最大周期: {cycle_stats['最大周期']:.6f}ms
最小周期: {cycle_stats['最小周期']:.6f}ms
周期抖动: {cycle_stats['周期抖动']:.6f}ms
"""
        filename = f"reports/report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(report)
        logger.info(f"测试报告已保存到 {filename}")
