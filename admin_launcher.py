import sys
import ctypes
import asyncio
from pathlib import Path
from loguru import logger
from tests.test_case4 import modbus_stress_test


def is_admin():
    """检查当前是否已是管理员权限"""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except AttributeError:
        return False  # 非Windows平台


def elevate_restart():
    """提权并重启当前进程"""
    if not is_admin():
        logger.warning("未检测到管理员权限，尝试提权...")

        # 获取当前执行路径
        executable = Path(sys.executable).resolve()
        script = Path(__file__).resolve()

        # 构造提权参数
        params = ' '.join([
            f'"{script}"',  # 当前脚本路径
            *[f'"{arg}"' for arg in sys.argv[1:]]  # 保留原有参数
        ])

        # 调用ShellExecute以管理员身份重启
        ret = ctypes.windll.shell32.ShellExecuteW(
            None,  # 父窗口句柄
            "runas",  # 操作类型（runas表示提权）
            f'"{executable}"',  # 可执行文件路径
            params,  # 参数
            None,  # 工作目录（None表示当前目录）
            1  # 显示窗口（SW_NORMAL）
        )

        # 返回值<=32表示错误
        if ret <= 32:
            error_codes = {
                2: "文件未找到",
                5: "拒绝访问",
                740: "需要提升权限"
            }
            logger.error(f"提权失败（错误码{ret}）: {error_codes.get(ret, '未知错误')}")
            sys.exit(ret)
        else:
            sys.exit(0)  # 正常退出当前进程


async def main():
    success = await modbus_stress_test(duration=12 * 3600)
    if not success:
        raise RuntimeError("Modbus测试失败")


if __name__ == "__main__":
    # Step 1: 权限检查与提权
    if sys.platform == 'win32':
        elevate_restart()

    # Step 2: 确认当前已是管理员
    logger.success(f"当前权限: {'管理员' if is_admin() else '标准用户'}")

    # Step 3: 运行业务代码
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("用户中断执行")
    except Exception as e:
        logger.critical(f"程序崩溃: {type(e).__name__}: {str(e)}")
        if sys.platform == 'win32':
            ctypes.windll.user32.MessageBoxW(
                0,
                f"程序异常: {str(e)}",
                "Modbus客户端错误",
                0x10
            )
