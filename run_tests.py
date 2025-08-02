import pytest
import time
from datetime import datetime
from loguru import logger


def main():
    logger.add("logs/test_run_{time}.log", rotation="100 MB", encoding="utf-8")

    start_time = time.time()
    logger.info(f"开始测试套件执行 - {datetime.now()}")

    # 执行测试用例1
    logger.info("执行测试用例1: 1ms精度Modbus压力测试")
    pytest.main(["-v", "tests/test_case1.py"])

    # 执行测试用例2
    logger.info("执行测试用例2: 多主站混合场景测试")
    pytest.main(["-v", "tests/test_case2.py"])

    duration = time.time() - start_time
    logger.success(f"测试套件执行完成 - 总耗时: {duration:.2f}秒")


if __name__ == "__main__":
    main()
