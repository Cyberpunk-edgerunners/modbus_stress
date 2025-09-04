import pytest
import asyncio
from loguru import logger
from pathlib import Path
import sys
from pymodbus import ModbusException
from config import settings

# 修复导入路径（确保可以找到core模块）
sys.path.insert(0, str(Path(__file__).parent.parent))
from core.realtime_async_client import HighPrecisionAsyncModbusClient

# 兼容性改造 --------------------------------------------------
async def modbus_stress_test(duration: int = 60):
    client = None
    try:
        # 从配置中获取要启动的客户端ID
        master_ids = list(settings.MASTER_CONFIGS.keys())[:settings.CONNECTION_POOL_SIZE]

        # 传递客户端ID列表
        client = HighPrecisionAsyncModbusClient(master_ids)
        logger.info(f"客户端初始化完成，将启动 {len(master_ids)} 个客户端")

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

