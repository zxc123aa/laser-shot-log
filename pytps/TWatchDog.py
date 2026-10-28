import os
import win32gui
import win32com.client
import pywintypes
from watchdog.events import FileSystemEventHandler
import TSolve
import win32con
import pyautogui
import time
from ctypes import windll, c_bool
import win32api


class MyHandler(FileSystemEventHandler):
    def __init__(self, app):
        self.app = app

    def on_created(self, event):
        path = event.src_path
        file_name = os.path.basename(path)
        time.sleep(0.5)
        if file_name.endswith(".tif") or file_name.endswith(".tiff") or file_name.endswith(
                ".jpg") or file_name.endswith(".png") or file_name.endswith(".raw"):
            if os.path.isfile(self.app.imageFileName):
                self.app.imageFig.clf()
                self.app.specFig.clf()
            self.app.imageFileName = os.path.join(self.app.imagePath, file_name)
            # 新图自动解谱，但截止能量只认手动光标：先清零
            self.app.isManualCursor = False
            self.app.specCursor = 0.0
            self.app.imageCursor = None
            TSolve.Solve(self.app)
            if hasattr(self.app, "ensureSavePath"):
                self.app.ensureSavePath()
            else:
                os.makedirs(os.path.dirname(self.app.onlineCutoffEnergyListName), exist_ok=True)
            with open(self.app.onlineCutoffEnergyListName, 'a') as f:
                ce = getattr(self.app, "specCursor", 0.0)
                if isinstance(ce, (int, float)) and ce > 0:
                    f.write(str(file_name) + "\t" + str(round(float(ce), 2)) + "MeV\n")
                else:
                    f.write(str(file_name) + "\t" + "待手动确认\n")
            autoTrigger()
        else:
            pass


def block_input(block=True):
    """阻止用户输入（鼠标和键盘）"""
    try:
        windll.user32.BlockInput(c_bool(block))
        return True
    except:
        print(f"Failed to {'block' if block else 'unblock'} input")
        return False


def force_set_foreground_window(hwnd, max_retries=5):
    """强制设置前台窗口（增强版，带重试）"""
    for retry in range(max_retries):
        try:
            if not win32gui.IsWindow(hwnd):
                print(f"Window handle {hwnd} is invalid")
                return False

            # 获取当前前台窗口的线程 ID
            current_foreground = win32gui.GetForegroundWindow()
            foreground_thread_id = windll.user32.GetWindowThreadProcessId(current_foreground, 0)
            # 获取目标窗口的线程 ID
            target_thread_id = windll.user32.GetWindowThreadProcessId(hwnd, 0)

            # 附加到前台窗口的线程
            if foreground_thread_id != target_thread_id:
                windll.user32.AttachThreadInput(foreground_thread_id, target_thread_id, True)

            # 先显示窗口
            win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

            # 设置前台窗口
            win32gui.SetForegroundWindow(hwnd)

            # 使用 SetWindowPos 强制置顶（临时）
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
            time.sleep(0.1)
            win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)

            # 分离线程
            if foreground_thread_id != target_thread_id:
                windll.user32.AttachThreadInput(foreground_thread_id, target_thread_id, False)

            # 验证是否成功
            time.sleep(0.2)
            if win32gui.GetForegroundWindow() == hwnd:
                return True
            else:
                print(f"Retry {retry + 1}/{max_retries}: Window not in foreground")
                time.sleep(0.3)

        except Exception as e:
            print(f"Retry {retry + 1}/{max_retries}: Error - {e}")
            time.sleep(0.3)

    print(f"Failed to set window to foreground after {max_retries} retries")
    return False


def wait_for_window_ready(hwnd, timeout=3):
    """等待窗口完全准备好"""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            if win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd):
                # 额外等待窗口稳定
                time.sleep(0.2)
                return True
        except:
            pass
        time.sleep(0.1)
    return False


def safe_window_operation(operation_func, window_name, *args, **kwargs):
    """安全的窗口操作包装器（带输入阻止）"""
    input_blocked = False
    try:
        # 阻止用户输入
        input_blocked = block_input(True)
        if input_blocked:
            print(f"Input blocked for {window_name} operation")
        else:
            print(f"WARNING: Could not block input (需要管理员权限)")

        time.sleep(0.2)  # 给系统时间处理阻止请求

        # 执行操作
        result = operation_func(*args, **kwargs)

        return result

    finally:
        # 无论如何都要解除输入阻止
        if input_blocked:
            time.sleep(0.2)
            block_input(False)
            print(f"Input unblocked after {window_name} operation")


def handle_andor_window(pytpsWindow):
    """处理 Andor 窗口的独立函数"""
    andorWindow = win32gui.FindWindow("OWL_Window", u"Andor SOLIS for Imaging: X-11812")

    if andorWindow == 0:
        return False

    def _andor_operation():
        try:
            # 显示并恢复窗口
            win32gui.ShowWindow(andorWindow, win32con.SW_RESTORE)
            time.sleep(0.3)

            # 等待窗口准备好
            if not wait_for_window_ready(andorWindow):
                print("Andor window not ready")
                return False

            # 强制设置前台（带重试）
            if not force_set_foreground_window(andorWindow, max_retries=5):
                print("Failed to set Andor window to foreground")
                return False

            # 再次验证
            time.sleep(0.3)
            current_window = win32gui.GetForegroundWindow()
            if current_window != andorWindow:
                print("ERROR: Andor window lost focus, aborting operation")
                return False

            # 执行操作
            pyautogui.FAILSAFE = False
            pyautogui.press('f5')
            print('Pressed F5 on Andor window')

            time.sleep(0.5)

            # 最小化
            win32gui.ShowWindow(andorWindow, win32con.SW_SHOWMINIMIZED)
            time.sleep(0.2)

            return True

        except Exception as e:
            print(f"Error in Andor operation: {e}")
            return False

    # 使用安全包装器执行
    result = safe_window_operation(_andor_operation, "Andor")

    # 恢复 PyTPS 窗口
    if pytpsWindow != 0:
        time.sleep(0.2)
        force_set_foreground_window(pytpsWindow, max_retries=3)

    return result


def handle_mosaic_window(pytpsWindow):
    """处理 Mosaic 窗口的独立函数"""
    hwnd = win32gui.FindWindow("Qt5QWindowIcon", u"Mosaic3.0")

    if hwnd == 0:
        return False

    def _mosaic_operation():
        try:
            # 显示窗口
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
            time.sleep(0.3)

            # 等待窗口准备好
            if not wait_for_window_ready(hwnd):
                print("Mosaic window not ready")
                return False

            # 设置前台（带重试）
            if not force_set_foreground_window(hwnd, max_retries=5):
                print("Failed to set Mosaic window to foreground")
                return False

            # 再次验证
            time.sleep(0.3)
            current_window = win32gui.GetForegroundWindow()
            if current_window != hwnd:
                print("ERROR: Mosaic window lost focus, aborting operation")
                return False

            # 执行点击
            pyautogui.click(115, 81)
            print('Clicked on Mosaic window')

            time.sleep(0.5)

            # 最小化
            win32gui.ShowWindow(hwnd, win32con.SW_SHOWMINIMIZED)
            time.sleep(0.2)

            return True

        except Exception as e:
            print(f"Error in Mosaic operation: {e}")
            return False

    # 使用安全包装器执行
    result = safe_window_operation(_mosaic_operation, "Mosaic")

    # 恢复 PyTPS 窗口
    if pytpsWindow != 0:
        time.sleep(0.2)
        force_set_foreground_window(pytpsWindow, max_retries=3)

    return result


def autoTrigger():
    """主触发函数"""
    pytpsWindow = win32gui.FindWindow("Qt672QWindowIcon", u"PyTPS")

    print("=" * 50)
    print("Starting autoTrigger operation...")
    print("=" * 50)

    try:
        # 按顺序处理窗口
        andor_success = handle_andor_window(pytpsWindow)
        print(f"Andor window operation: {'SUCCESS' if andor_success else 'FAILED'}")

        time.sleep(0.5)

        mosaic_success = handle_mosaic_window(pytpsWindow)
        print(f"Mosaic window operation: {'SUCCESS' if mosaic_success else 'FAILED'}")

        # 最后确保 PyTPS 在前台
        if pytpsWindow != 0:
            time.sleep(0.3)
            force_set_foreground_window(pytpsWindow, max_retries=3)

        print("=" * 50)
        print("autoTrigger operation completed")
        print("=" * 50)

    except Exception as e:
        print(f"Critical error in autoTrigger: {e}")
        # 即使出错也尝试恢复 PyTPS 窗口
        if pytpsWindow != 0:
            try:
                force_set_foreground_window(pytpsWindow, max_retries=3)
            except:
                pass
