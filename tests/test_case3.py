import pytest
import asyncio
from core.async_client import HighPrecisionAsyncModbusClient
from config import settings
from loguru import logger


@pytest.mark.asyncio
async def test_async_long_connection():
    """终极版长连接测试"""
    client = None
    test_passed = False

    try:
        client = HighPrecisionAsyncModbusClient()
        await client.run_test(duration=settings.TEST_DURATION)

        stats = client.stats
        assert stats["成功请求"] / stats["总请求数"] > 0.99, "成功率低于99%"
        assert stats["周期统计"]["平均周期"] < 5.0, f"平均周期过高: {stats['周期统计']['平均周期']}ms"

        test_passed = True

    except Exception as e:
        logger.critical(f"测试失败: {type(e).__name__}: {str(e)}")
        raise
    finally:
        if client:
            try:
                # 确保清理无论测试通过与否都会执行
                await client.cleanup()
            except Exception as e:
                # 如果测试已通过但清理失败，只记录错误不失败测试
                if test_passed:
                    logger.error(f"清理失败但不影响测试结果: {type(e).__name__}")
                else:
                    raise


if __name__ == "__main__":
    pytest.main(["-s", "tests/test_case3.py"])

