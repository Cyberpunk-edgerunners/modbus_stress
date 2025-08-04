import time
import ctypes
import random
import socket
from datetime import datetime
from itertools import cycle

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
            "周期记录": []
        }

    # def _setup_client(self):
    #     """配置客户端 (兼容PyModbus 3.x)"""
    #     self.client = ModbusTcpClient(
    #         host=self.host,
    #         port=self.port,
    #         timeout=1.0,
    #         no_delay=settings.DISABLE_NAGLE  # 禁用Nagle的新方式
    #     )
    #
    #     if not self.client.connect():
    #         logger.error("初始连接失败")
    #         raise ConnectionError("无法建立初始连接")
        self.kernel32 = ctypes.WinDLL('kernel32')
        self.frequency = ctypes.c_int64()
        self.kernel32.QueryPerformanceFrequency(ctypes.byref(self.frequency))

    def _busy_wait_1ms(self, timeout=5.0):
        """带动态调节的高精度忙等待"""
        start_counter = ctypes.c_int64()
        self.kernel32.QueryPerformanceCounter(ctypes.byref(start_counter))
        target = start_counter.value + (self.frequency.value // 1000)

        min_sleep = 0.00001  # 10μs
        max_sleep = 0.0005  # 500μs
        last_error = 1.0  # 初始误差1ms

        while True:
            now = ctypes.c_int64()
            self.kernel32.QueryPerformanceCounter(ctypes.byref(now))

            # 计算剩余时间（毫秒）
            remaining_ms = (target - now.value) * 1000 / self.frequency.value

            # 完成条件
            if remaining_ms <= 0:
                actual_ms = (now.value - start_counter.value) * 1000 / self.frequency.value
                return actual_ms

            # 超时保护
            elapsed = (now.value - start_counter.value) / self.frequency.value
            if elapsed > timeout:
                raise TimeoutError(f"忙等待超时 {timeout}s")

            # 动态睡眠（基于剩余时间比例）
            sleep_time = min(
                max(min_sleep, remaining_ms * last_error * 0.5),  # 按上次误差比例调整
                max_sleep
            )
            time.sleep(sleep_time)
            last_error = remaining_ms  # 记录本次误差

    def _busy_wait(self, target_delay):
        """高精度忙等待"""
        start = time.perf_counter()
        while time.perf_counter() - start < target_delay:
            pass

    def _random_operation(self, conn):
        # """每次操作前验证连接"""
        # try:
        #     if not conn.is_socket_open():
        #         logger.warning("连接已断开，尝试重连...")
        #         conn.connect()

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


    def run_test(self, duration):
        """带连接健康监测的压力测试"""
        logger.info("=== 调试模式启动 ===")
        logger.info(f"连接池状态: {len(self.pool._pool)}个可用连接")  # 确保连接池数据

        # 测试连接是否真的可用
        test_conn = self.pool.get_connection()
        logger.info(f"测试连接ID: {id(test_conn)}, 是否存活: {test_conn.is_socket_open()}")
        self.pool.release_connection(test_conn)

        logger.info(f"启动压力测试，持续时间: {duration}秒")
        end_time = time.time() + duration
        last_status_time = time.time()

        try:
            while time.time() < end_time:
                # 在每次操作后增加基础延迟
                time.sleep(0.001)  # 1ms基础间隔

                cycle_start = time.perf_counter()
                conn = None
                conn_start = time.time()

                try:
                    # 获取连接（带超时监控）
                    conn = self.pool.get_connection()
                    get_conn_time = time.time() - conn_start

                    if get_conn_time > 0.1:  # 连接获取超过100ms警告
                        logger.warning(f"获取连接耗时: {get_conn_time * 1000:.1f}ms")

                    # 执行操作
                    op_start = time.time()
                    if not self._random_operation(conn):
                        self._handle_connection_error(conn)
                        continue

                    op_time = time.time() - op_start
                    if op_time > 0.05:  # 单次操作超过50ms警告
                        logger.warning(f"操作耗时: {op_time * 1000:.1f}ms")

                except ModbusException as e:
                    logger.error(f"Modbus协议错误: {e}")
                    time.sleep(0.1)
                except socket.timeout:
                    logger.error("网络操作超时")
                    self.pool._clean_broken_connections()  # 新增方法清理失效连接
                except Exception as e:
                    logger.critical(f"未处理异常: {type(e).__name__} - {e}")
                    break
                finally:
                    if conn:
                        self.pool.release_connection(conn)

                # 周期控制
                try:
                    actual_ms = self._busy_wait_1ms()
                    self.stats["周期记录"].append(actual_ms)

                    # 状态打印（每秒更新）
                    if time.time() - last_status_time >= 1.0:
                        last_status_time = time.time()
                        avg = sum(self.stats["周期记录"][-100:]) / len(self.stats["周期记录"][-100:])
                        print(
                            f"\r实际周期: {actual_ms:.3f}ms | "
                            f"平均: {avg:.3f}ms | "
                            f"操作成功率: {self.stats['成功请求'] / self.stats['总请求数'] * 100:.1f}%",
                            end=""
                        )

                except TimeoutError as e:
                    logger.error(f"周期控制失败: {e}")
                    time.sleep(0.01)  # 发生错误时短暂休眠

        except KeyboardInterrupt:
            logger.info("用户手动终止测试")
        finally:
            self._generate_report()
            # 增加资源释放
            if hasattr(self, 'kernel32'):
                del self.kernel32

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
        cycles = self.stats["周期记录"]

        avg_cycle = sum(cycles) / len(cycles) if cycles else 0
        max_cycle = max(cycles) if cycles else 0
        min_cycle = min(cycles) if cycles else 0
        jitter = max_cycle - min_cycle

        avg_latency = sum(latencies) / len(latencies) if latencies else 0
        max_latency = max(latencies) if latencies else 0

        report = f"""
=== Modbus压力测试报告 ===
测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
运行时长: {duration:.2f}秒
总请求数: {self.stats["总请求数"]}
成功请求: {self.stats["成功请求"]}
失败请求: {self.stats["失败请求"]}
QPS: {qps:.2f}
成功率: {success_rate:.2f}%
--- 周期统计 ---
平均周期: {avg_cycle:.6f}ms
最大周期: {max_cycle:.6f}ms
最小周期: {min_cycle:.6f}ms
周期抖动: {jitter:.6f}ms
--- 延迟统计 ---
平均延迟: {avg_latency:.2f}ms
最大延迟: {max_latency:.2f}ms
"""
        filename = f"reports/report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(report)
        logger.info(f"测试报告已保存到 {filename}")
