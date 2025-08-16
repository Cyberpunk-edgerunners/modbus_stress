import pytest
import asyncio
from loguru import logger
from pathlib import Path
import sys
from pymodbus import ModbusException

# 修复导入路径（确保可以找到core模块）
sys.path.insert(0, str(Path(__file__).parent.parent))
from core.realtime_async_client import HighPrecisionAsyncModbusClient

# 兼容性改造 --------------------------------------------------
async def modbus_stress_test(duration: int = 60):
    client = None
    try:
        client = HighPrecisionAsyncModbusClient()
        logger.info("客户端初始化完成")

        # 连接池健康检查
        if not await client.pool.validate_pool_health():
            raise RuntimeError("连接池初始化失败")

        # pymodbus连接测试方式
        for attempt in range(3):
            try:
                conn = await client.pool.get_connection()
                # 使用关键字参数调用
                result = await conn.read_holding_registers(address=0, count=1)
                if result.isError():
                    raise ModbusException(str(result))
                logger.success("Modbus连接测试通过")
                break
            except Exception as e:
                if attempt == 2:
                    raise RuntimeError(f"Modbus连接失败: {e}")
                logger.warning(f"连接尝试 {attempt+1}/3 失败: {str(e)}")
                await asyncio.sleep(1)

        await client.run_test(duration)
        return True
    except Exception as e:
        logger.opt(exception=True).error("测试致命错误")
        return False
    finally:
        if client:
            await client.cleanup()

# 保留原有pytest测试用例 ---------------------------------------
@pytest.fixture(scope="module")
def event_loop():
    """创建事件循环（pytest专用）"""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()

@pytest.mark.asyncio
async def test_modbus_performance():
    """pytest测试入口"""
    assert await modbus_stress_test(duration=5)  # 测试5秒

# 命令行直接运行支持 -------------------------------------------
if __name__ == "__main__":
    async def _main():
        duration = int(sys.argv[1]) if len(sys.argv) > 1 else 10
        await modbus_stress_test(duration)

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        logger.info("用户中断测试")
    except Exception as e:
        logger.critical(f"致命错误: {e}")
        raise

