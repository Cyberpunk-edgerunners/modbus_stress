from core.connection import ModbusConnectionPool
from loguru import logger
import time
import sys


def test_holding_registers():
    logger.info("=== 兼容性保持寄存器测试 ===")

    # 测试用例（移除slave参数）
    TEST_CASES = [
        {"address": 0, "count": 1, "desc": "保持寄存器测试"},
        {"address": 1, "count": 2, "desc": "批量读取测试"}
    ]

    pool = ModbusConnectionPool()

    for case in TEST_CASES:
        conn = None
        try:
            conn = pool.get_connection()
            logger.info(f"测试: {case['desc']} [地址:{case['address']}]")

            # PyModbus 3.x 兼容调用
            result = conn.read_holding_registers(
                address=case["address"],
                count=case["count"]
            )

            if result.isError():
                logger.error(f"响应错误: {result}")
            else:
                logger.success(f"读取成功: {result.registers}")

        except Exception as e:
            logger.error(f"测试失败: {str(e)}")
        finally:
            if conn:
                pool.release_connection(conn)
            time.sleep(1)  # 确保对端释放资源

    logger.info("=== 测试完成 ===")


if __name__ == "__main__":
    logger.remove()
    logger.add(
        sys.stdout,
        format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
               "<level>{level: <8}</level> | "
               "<cyan>{module}</cyan> - "
               "<level>{message}</level>",
        level="DEBUG"
    )

    test_holding_registers()
