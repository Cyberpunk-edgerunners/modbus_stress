from core.connection import ModbusConnectionPool
from loguru import logger
import time


def test_connection():
    logger.info("=== 开始增强版连接测试 ===")

    # 测试参数
    test_cases = [
        # {"address": 1, "count": 1, "desc": "输入寄存器测试"},
        {"address": 1, "count": 2, "desc": "保持寄存器测试"}
    ]

    pool = ModbusConnectionPool()

    for i, test in enumerate(test_cases, 1):
        conn = None
        try:
            conn = pool.get_connection()
            logger.info(f"测试#{i}: {test['desc']} - 地址:{test['address']} 数量:{test['count']}")

            # 添加延迟确保连接稳定
            time.sleep(0.1)

            result = conn.read_holding_registers(
                address=test["address"],
                count=test["count"]
            )

            if result.isError():
                logger.error(f"测试#{i} 错误响应: {str(result)}")
            else:
                logger.success(f"测试#{i} 成功 - 值: {result.registers}")

        except Exception as e:
            logger.error(f"测试#{i} 失败: {type(e).__name__}: {str(e)}")
            logger.debug("完整堆栈:", exc_info=e)
        finally:
            if conn:
                pool.release_connection(conn)
            time.sleep(0.5)  # 连接间延迟

    logger.info("=== 测试完成 ===")


if __name__ == "__main__":
    test_connection()
    time.sleep(1)  # 确保日志输出完成
