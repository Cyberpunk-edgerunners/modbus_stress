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
            timeout=1.0
        )

        # 手动设置TCP_NODELAY
        if not self.client.connect():
            raise ConnectionError("无法建立初始连接")

        if hasattr(self.client, 'socket') and self.client.socket:
            self.client.socket.setsockopt(
                socket.IPPROTO_TCP,
                socket.TCP_NODELAY,
                1  # 1=禁用Nagle算法
            )

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
        """使用长连接运行压力测试（完整版）"""
        logger.info("开始Modbus长连接压力测试...")
        end_time = time.time() + duration
        conn = None

        try:
            # 获取长连接（整个测试周期共用）
            conn = self.pool.get_persistent_connection()

            while time.time() < end_time:
                cycle_start = time.time()

                try:
                    # 第一次操作
                    if not self._random_operation(conn):
                        self._handle_connection_error(conn)

                    # 第二次操作（保持连接复用）
                    if not self._random_operation(conn):
                        self._handle_connection_error(conn)

                except Exception as e:
                    logger.error(f"测试发生异常: {e}")
                    self._save_error_report(e)
                    # 异常后自动获取新连接
                    conn = self.pool.get_persistent_connection()

                # 更新统计
                cycle_time = time.time() - cycle_start
                self._update_cycle_stats(cycle_time)

                # 定期打印统计
                if len(self.stats["周期记录"]) % 100 == 0:
                    self._print_cycle_stats()

        except KeyboardInterrupt:
            logger.warning("测试被手动中断")
        finally:
            # 确保生成最终报告（关键补充）
            self._generate_report()  # ✅ 确保报告生成

            # 可选：保持长连接供后续使用
            # 如需立即关闭可取消注释下行
            self.pool.close_persistent_connection()

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
