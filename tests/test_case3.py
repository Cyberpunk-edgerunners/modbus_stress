import pytest
import asyncio
from core.async_client import HighPrecisionAsyncModbusClient
from config import settings


@pytest.mark.asyncio
async def test_async_long_connection():
    """测试异步长连接性能"""
    async with HighPrecisionAsyncModbusClient() as client:
        await client.run_test(duration=settings.TEST_DURATION)

        stats = client.stats
        assert stats["成功请求"] / stats["总请求数"] > 0.99, "成功率低于99%"
        assert stats["周期统计"]["平均周期"] < 2.0, "平均周期超过5ms"
        assert stats["周期统计"]["周期抖动"] < 2.0, "周期抖动过大"


if __name__ == "__main__":
    pytest.main(["-s", "tests/test_case3.py"])

