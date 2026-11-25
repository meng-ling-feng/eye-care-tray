"""护眼小助手：pywebview + pystray + Windows Gamma Ramp。

界面由 Vue3 构建（dist/），渲染使用系统 WebView2；
托盘常驻、色温护眼，退出自动还原 Gamma。
"""

import ctypes
import json
import math
import os
import sys
import threading
import time
import winreg
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pystray
import webview
from PIL import Image

APP_NAME = "护眼小助手"
MUTEX_NAME = "EyeCareTray_SingleInstance"

DEFAULT_CONFIG = {
    "percent": 50,
}


def resource_base():
    """PyInstaller 打包后资源在 _MEIPASS，开发时在脚本目录。"""
    if getattr(sys, "frozen", False):
        return sys._MEIPASS  # noqa: SLF001
    return os.path.dirname(os.path.abspath(__file__))


def dist_dir():
    # 打包时前端以 --add-data "web/dist;web/dist" 放入 _MEIPASS/web/dist
    bundled = os.path.join(resource_base(), "web", "dist")
    if os.path.isdir(bundled):
        return bundled
    return os.path.join(resource_base(), "dist")


def icon_path():
    return os.path.join(resource_base(), "icon.ico")


def config_paths():
    base = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "EyeCareTray")
    return base, os.path.join(base, "config.json")


CONFIG_DIR, CONFIG_PATH = config_paths()


def clamp(value, low, high):
    return max(low, min(high, value))


def window_center_pos(width, height):
    """计算主屏正中的窗口左上角坐标（逻辑像素，pywebview 内部按 DPI 换算）。

    pywebview 在 Windows 下会提前创建 Form 句柄，导致 StartPosition=CenterScreen
    失效；显式传 x/y 才会走 Location 分支，所以这里自己算中心点。
    """
    try:
        screen = webview.screens[0]
        return (
            int(screen.x + (screen.width - width) // 2),
            int(screen.y + (screen.height - height) // 2),
        )
    except Exception:  # noqa: BLE001
        return None, None


# ---------------- Gamma ----------------

GAMMA_RAMP = ctypes.c_ushort * 256 * 3
DISPLAY_DEVICE_ATTACHED_TO_DESKTOP = 0x00000001


class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("DeviceName", ctypes.c_wchar * 32),
        ("DeviceString", ctypes.c_wchar * 128),
        ("StateFlags", ctypes.c_ulong),
        ("DeviceID", ctypes.c_wchar * 128),
        ("DeviceKey", ctypes.c_wchar * 128),
    ]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

gdi32.GetDeviceGammaRamp.argtypes = [ctypes.c_void_p, ctypes.POINTER(GAMMA_RAMP)]
gdi32.GetDeviceGammaRamp.restype = ctypes.c_long
gdi32.SetDeviceGammaRamp.argtypes = [ctypes.c_void_p, ctypes.POINTER(GAMMA_RAMP)]
gdi32.SetDeviceGammaRamp.restype = ctypes.c_long
gdi32.CreateDCW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_void_p]
gdi32.CreateDCW.restype = ctypes.c_void_p
gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
user32.GetDC.argtypes = [ctypes.c_void_p]
user32.GetDC.restype = ctypes.c_void_p
user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.EnumDisplayDevicesW.argtypes = [
    ctypes.c_wchar_p, ctypes.c_ulong, ctypes.POINTER(DISPLAY_DEVICEW), ctypes.c_ulong
]


def kelvin_to_rgb(kelvin):
    """Tanner Helland 色温近似算法。"""
    n = kelvin / 100.0
    if n <= 66:
        red = 255.0
        green = 99.4708025861 * math.log(n) - 161.1195681661
    else:
        red = 329.698727446 * ((n - 60) ** -0.1332047592)
        green = 288.1221695283 * ((n - 60) ** -0.0755148492)
    if n >= 66:
        blue = 255.0
    elif n <= 19:
        blue = 0.0
    else:
        blue = 138.5177312231 * math.log(n - 10) - 305.0447927307
    return clamp(red, 0, 255), clamp(green, 0, 255), clamp(blue, 0, 255)


REF_RGB = kelvin_to_rgb(6500)


def temperature_factors(temperature):
    rgb = kelvin_to_rgb(clamp(temperature, 1000, 12000))
    return tuple(clamp(rgb[i] / REF_RGB[i], 0, 1) for i in range(3))


class GammaManager:
    """保存每个显示器的原始 Gamma，按色温比例调节，退出时还原。"""

    def __init__(self):
        self.original = {}
        self.available = False
        self._enumerate_displays()
        self._read_originals()

    def _enumerate_displays(self):
        devices = []
        index = 0
        while True:
            device = DISPLAY_DEVICEW()
            device.cb = ctypes.sizeof(DISPLAY_DEVICEW)
            if not user32.EnumDisplayDevicesW(None, index, ctypes.byref(device), 0):
                break
            if device.StateFlags & DISPLAY_DEVICE_ATTACHED_TO_DESKTOP:
                devices.append(device.DeviceName)
            index += 1
        self.devices = devices or [None]

    def _open_dc(self, device_name):
        if device_name is None:
            return user32.GetDC(None), False
        return gdi32.CreateDCW(None, device_name, None, None), True

    def _close_dc(self, hdc, created):
        if not hdc:
            return
        if created:
            gdi32.DeleteDC(hdc)
        else:
            user32.ReleaseDC(None, hdc)

    @staticmethod
    def _is_non_neutral(ramp):
        """检测当前曲线是否偏离中性（上次进程被强杀残留的暖色）。"""
        for channel in range(3):
            if ramp[channel][255] < 60000:
                return True
        return False

    def _read_originals(self):
        ok = True
        for name in self.devices:
            hdc, created = self._open_dc(name)
            if not hdc:
                ok = False
                continue
            try:
                ramp = GAMMA_RAMP()
                if not gdi32.GetDeviceGammaRamp(hdc, ctypes.byref(ramp)):
                    ok = False
                    continue
                if self._is_non_neutral(ramp):
                    # 残留了脏底色：先重置为中性曲线，再把中性当作原始状态
                    neutral = GAMMA_RAMP()
                    for channel in range(3):
                        for index in range(256):
                            neutral[channel][index] = index * 257
                    gdi32.SetDeviceGammaRamp(hdc, ctypes.byref(neutral))
                    time.sleep(0.05)
                    gdi32.GetDeviceGammaRamp(hdc, ctypes.byref(ramp))
                self.original[name] = ramp
            except Exception:  # noqa: BLE001
                ok = False
            finally:
                self._close_dc(hdc, created)
        self.available = ok and bool(self.original)

    @staticmethod
    def _build_ramp(original, factors, min_endpoint, max_exponent):
        """按原始曲线生成护眼曲线：端点受 Windows 限制，蓝光用指数进一步压低。"""
        ramp = GAMMA_RAMP()
        extra = (0.0, (1.0 - factors[1]) * 0.4, (1.0 - factors[2]) * 0.8)
        for channel in range(3):
            end = max(min_endpoint, min(65535, int(original[channel][255] * factors[channel])))
            base = original[channel][255] or 1
            exponent = min(max_exponent, 1.0 + extra[channel])
            for index in range(256):
                shape = original[channel][index] / base
                ramp[channel][index] = min(65535, int(end * (shape ** exponent)))
            # Windows 要求 Gamma 表严格递增。
            for index in range(1, 256):
                if ramp[channel][index] <= ramp[channel][index - 1]:
                    ramp[channel][index] = min(65535, ramp[channel][index - 1] + 1)
        return ramp

    def set_filter(self, percent):
        """0% = 6500K（正常）~ 100% = 3400K（最暖）。"""
        if not self.available:
            return False
        temperature = 6500 - 31 * clamp(int(percent), 0, 100)
        factors = temperature_factors(temperature)
        for min_endpoint, max_exponent in ((36000, 2.0), (45000, 1.5), (52000, 1.0)):
            success = True
            for name, original in self.original.items():
                hdc, created = self._open_dc(name)
                if not hdc:
                    success = False
                    break
                try:
                    ramp = self._build_ramp(original, factors, min_endpoint, max_exponent)
                    if not gdi32.SetDeviceGammaRamp(hdc, ctypes.byref(ramp)):
                        success = False
                except Exception:  # noqa: BLE001
                    success = False
                finally:
                    self._close_dc(hdc, created)
            if success:
                return True
            self.restore()
        return False

    def restore(self):
        if not self.available:
            return
        for name, original in self.original.items():
            hdc, created = self._open_dc(name)
            if not hdc:
                continue
            try:
                gdi32.SetDeviceGammaRamp(hdc, ctypes.byref(original))
            except Exception:  # noqa: BLE001
                pass
            finally:
                self._close_dc(hdc, created)


# ---------------- 配置 ----------------

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
        for key in cfg:
            if key in saved:
                cfg[key] = saved[key]
    except Exception:  # noqa: BLE001
        pass
    cfg["percent"] = clamp(int(cfg["percent"]), 0, 100)
    return cfg


def save_config(cfg):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass


# ---------------- 开机自启动 ----------------

AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_command():
    """写入 Run 项的命令行：打包版指向自身，源码版用 pythonw 启动脚本。"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    return f'"{pythonw}" "{os.path.abspath(__file__)}"'


def get_autostart():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as key:
            value, _ = winreg.QueryValueEx(key, APP_NAME)
            return bool(value)
    except OSError:
        return False


def set_autostart(enabled):
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0, winreg.KEY_WRITE
        ) as key:
            if enabled:
                winreg.SetValueEx(
                    key, APP_NAME, 0, winreg.REG_SZ, autostart_command()
                )
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False


def single_instance_mutex():
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, True, MUTEX_NAME)
    return handle if kernel32.GetLastError() != 183 else None


# ---------------- 应用主体 ----------------

class App:
    def __init__(self):
        self.mutex = single_instance_mutex()
        self.gamma = GammaManager()
        self.config = load_config()
        self.window = None
        self.tray = None
        self.quitting = False
        self._window_visible = True
        self._lock = threading.Lock()

    # ---- 前端可调用接口 ----

    def get_config(self):
        return dict(self.config)

    def set_config(self, patch):
        with self._lock:
            if "percent" in patch:
                self.config["percent"] = clamp(int(patch["percent"]), 0, 100)
            save_config(self.config)
        self.apply_eye()
        self.push("config:changed", dict(self.config))

    def _hwnd(self):
        """pywebview/winforms 返回的是 .NET IntPtr，需显式转换。"""
        if not self.window or not self.window.native:
            return None
        handle = self.window.native.Handle
        if hasattr(handle, "ToInt64"):
            return int(handle.ToInt64())
        return int(handle)

    def quit_app(self):
        self.quitting = True
        threading.Thread(target=self._shutdown, daemon=True).start()

    # ---- 内部 ----

    def apply_eye(self):
        if not self.gamma.available:
            self.push("gamma:status", False)
            return
        try:
            ok = self.gamma.set_filter(self.config["percent"])
        except Exception:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            ok = False
        self.push("gamma:status", ok)

    def _autotest(self):
        """--autotest 模式：模拟前端在运行时调用 set_config 并读回屏幕曲线。"""
        import ctypes as _ct

        def _read_ends():
            ramp = GAMMA_RAMP()
            hdc = user32.GetDC(None)
            gdi32.GetDeviceGammaRamp(hdc, _ct.byref(ramp))
            user32.ReleaseDC(None, hdc)
            return ramp[0][255], ramp[1][255], ramp[2][255]

        def _run():
            def step(label, patch):
                t = threading.Thread(
                    target=self.set_config, args=(patch,), daemon=True
                )
                t.start()
                t.join(10)
                time.sleep(1.5)
                print(f"[autotest] {label} -> screen { _read_ends() }", flush=True)

            try:
                print("[autotest] baseline ->", _read_ends(), flush=True)
                startup_hwnd = self._hwnd()
                startup_rect = RECT()
                user32.GetWindowRect(startup_hwnd, ctypes.byref(startup_rect))
                print(
                    f"[autotest] startup rect -> ({startup_rect.left},{startup_rect.top})",
                    flush=True,
                )
                # 验证窗口 move（绝对坐标）链路可用
                before = (self.window.x, self.window.y)
                self.window.move(before[0] + 40, before[1] + 30)
                time.sleep(1)
                print(f"[autotest] move {before} -> {(self.window.x, self.window.y)}", flush=True)

                # 模拟真实拖拽：系统原生标题栏按下左键 -> Windows 自动移动窗口 -> 松开
                try:
                    hwnd = self._hwnd()

                    def _rect():
                        rect = RECT()
                        user32.GetWindowRect(hwnd, ctypes.byref(rect))
                        return rect.left, rect.top

                    before_drag = _rect()
                    sx, sy = before_drag[0] + 180, before_drag[1] + 15
                    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                    # 后台进程抢焦点可能被 Windows 拒绝，重试到窗口真正置前再注入点击
                    for _ in range(10):
                        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)
                        user32.SetForegroundWindow(hwnd)
                        if user32.GetForegroundWindow() == hwnd:
                            break
                        time.sleep(0.1)
                    user32.SetCursorPos(sx, sy)
                    time.sleep(0.3)
                    user32.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
                    try:
                        time.sleep(0.3)
                        for offset in range(1, 9):
                            user32.SetCursorPos(sx + offset * 12, sy + offset * 8)
                            time.sleep(0.08)
                    finally:
                        user32.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP
                    time.sleep(0.5)
                    print(f"[autotest] drag {before_drag} -> {_rect()}", flush=True)
                except Exception as exc:  # noqa: BLE001
                    print(f"[autotest] drag failed: {exc}", flush=True)

                # 模拟前端真实调用：走 pywebview 桥接（JS 对象参数）
                self.window.evaluate_js("window.pywebview.api.set_config({percent: 80});")
                time.sleep(3)
                print("[autotest] js-bridge set -> screen", _read_ends(), flush=True)
                step("py set 80%", {"percent": 80})
                step("set 20%", {"percent": 20})
                step("set 0%", {"percent": 0})
            finally:
                print("[autotest] done, quitting", flush=True)
                self.quit_app()

        threading.Thread(target=_run, daemon=True).start()

    def push(self, event, payload):
        """向前端推送事件（主窗口存在时）。"""
        if not self.window:
            return
        try:
            self.window.evaluate_js(
                f"window.__push({json.dumps(event)},{json.dumps(payload)})"
            )
        except Exception:  # noqa: BLE001
            pass

    def toggle_window(self):
        if not self.window:
            return
        if self._window_visible:
            self.window.hide()
            self._window_visible = False
        else:
            self.window.show()
            self._window_visible = True
            # pywebview 的 show() 不保证把窗口提到前台，显式激活一下
            hwnd = self._hwnd()
            if hwnd:
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                user32.SetForegroundWindow(hwnd)
        self.update_tray_menu()

    def _make_tray_image(self):
        try:
            return Image.open(icon_path()).convert("RGBA").resize((64, 64))
        except Exception:  # noqa: BLE001
            image = Image.new("RGBA", (64, 64), (47, 124, 214, 255))
            return image

    def update_tray_menu(self):
        if not self.tray:
            return
        menu = pystray.Menu(
            pystray.MenuItem("显示 / 隐藏主窗口", self.toggle_window, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", lambda: self.quit_app()),
        )
        self.tray.menu = menu

    def start_tray(self):
        self.tray = pystray.Icon(
            APP_NAME,
            icon=self._make_tray_image(),
            title=APP_NAME,
        )
        self.update_tray_menu()
        threading.Thread(target=self.tray.run, daemon=True).start()

    def _shutdown(self):
        try:
            self.gamma.restore()
        except Exception:  # noqa: BLE001
            pass
        if self.tray:
            try:
                self.tray.stop()
            except Exception:  # noqa: BLE001
                pass
        if self.window:
            try:
                self.window.destroy()
            except Exception:  # noqa: BLE001
                pass
        os._exit(0)

    def run(self):
        if not self.mutex:
            return
        # 本地静态服务，加载 Vue 构建产物
        self.port = _serve(dist_dir())

        center_x, center_y = window_center_pos(880, 560)
        self.window = webview.create_window(
            APP_NAME,
            url=f"http://127.0.0.1:{self.port}/",
            js_api=_ApiBridge(self),
            width=880,
            height=560,
            x=center_x,
            y=center_y,
            resizable=False,
            background_color="#26282d",
        )
        self.window.events.closing += self._on_closing
        if "--autotest" in sys.argv:
            self.window.events.loaded += self._autotest

        self.start_tray()

        if self.gamma.available:
            self.gamma.set_filter(self.config["percent"])

        webview.start()
        if not self.quitting:
            self._shutdown()

    def _on_closing(self):
        """点关闭 = 收进托盘，不退出。"""
        if self.quitting:
            return True
        self.window.hide()
        self._window_visible = False
        self.update_tray_menu()
        return False  # 阻止真正关闭


class _ApiBridge:
    """暴露给前端 window.pywebview.api 的方法。"""

    def __init__(self, app):
        self._app = app

    def get_config(self):
        return self._app.get_config()

    def set_config(self, patch):
        threading.Thread(
            target=self._app.set_config, args=(patch or {},), daemon=True
        ).start()
        return True

    def quit_app(self):
        self._app.quit_app()
        return True

    def get_autostart(self):
        return get_autostart()

    def set_autostart(self, enabled):
        return set_autostart(bool(enabled))


def _serve(root):
    """静态服务 Vue 构建产物（避免 file:// 的跨域限制）。"""
    handler = lambda *args, **kwargs: SimpleHTTPRequestHandler(  # noqa: E731
        *args, directory=root, **kwargs
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return port


def main():
    app = App()
    app.run()


if __name__ == "__main__":
    main()
