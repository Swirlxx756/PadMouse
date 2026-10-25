"""
PadMouse —— 用手柄代替鼠标（Windows 托盘程序）

支持 XInput 手柄：Xbox 手柄、以及切到 XInput/Xbox 模式的大多数第三方手柄。
（PS 手柄可借助 DS4Windows / Steam 转成 XInput 后使用。）
依赖：pip install pystray pillow

运行：  pythonw pad_mouse.py              以右下角托盘图标运行（无窗口）
        pythonw pad_mouse.py --settings   打开改键设置窗口
        pythonw pad_mouse.py --overlay    在屏幕右下角显示按键一览小窗
        python pad_mouse.py --check       只检测手柄是否连接

托盘图标：绿色 = 运行中，灰色 = 已暂停，红色 = 没有手柄。
左键单击图标 = 暂停/恢复；右键菜单：按键一览、改键设置、退出。

改键：托盘右键 →「改键设置…」。每个键都可以设成鼠标键、键盘按键或组合键、
暂停、慢速/加速等，也可以设成「无（空键）」让它什么都不做。
键盘按键在弹出的屏幕键盘上点选。保存后立即生效，
设置存放在 %LOCALAPPDATA%\\PadMouse\\config.json。

Back = Xbox 徽标左边的「两个方框」键（View）
Start = Xbox 徽标右边的「三条横线」键（Menu）
"""

import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback

# ======================= 可调参数 =======================
MOUSE_CURVE = 2.2           # 加速曲线指数，越大小幅推动越精细
SLOW_FACTOR = 0.3           # 「精准慢速」按住时的速度倍率
FAST_FACTOR = 2.0           # 「加速」按住时的速度倍率
DEADZONE_LEFT = 7849        # 左摇杆死区（XInput 推荐值）
DEADZONE_RIGHT = 8689       # 右摇杆死区
TRIGGER_THRESHOLD = 30      # 扳机按下阈值 (0-255)
POLL_HZ = 144               # 轮询频率
REPEAT_DELAY = 0.40         # 按住后开始连发的延迟（秒）
REPEAT_RATE = 0.05          # 连发间隔（秒）
VIBRATE_FEEDBACK = True     # 暂停/恢复时震动提示
# ========================================================

DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA", "."), "PadMouse")
LOG_PATH = os.path.join(DATA_DIR, "padmouse.log")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
SETTINGS_TITLE = "PadMouse 改键设置"
OVERLAY_TITLE = "PadMouse 按键一览"
INSTANCE_MUTEX = "Local\\PadMouse_SingleInstance"

# ---------------- XInput ----------------
class XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [("wButtons", wt.WORD),
                ("bLeftTrigger", ctypes.c_ubyte),
                ("bRightTrigger", ctypes.c_ubyte),
                ("sThumbLX", ctypes.c_short),
                ("sThumbLY", ctypes.c_short),
                ("sThumbRX", ctypes.c_short),
                ("sThumbRY", ctypes.c_short)]


class XINPUT_STATE(ctypes.Structure):
    _fields_ = [("dwPacketNumber", wt.DWORD),
                ("Gamepad", XINPUT_GAMEPAD)]


class XINPUT_VIBRATION(ctypes.Structure):
    _fields_ = [("wLeftMotorSpeed", wt.WORD),
                ("wRightMotorSpeed", wt.WORD)]


def _load_xinput():
    for name in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
        try:
            return ctypes.WinDLL(name)
        except OSError:
            continue
    sys.exit("找不到 XInput 库，无法读取手柄。")


xinput = _load_xinput()
xinput.XInputGetState.argtypes = [wt.DWORD, ctypes.POINTER(XINPUT_STATE)]
xinput.XInputGetState.restype = wt.DWORD
xinput.XInputSetState.argtypes = [wt.DWORD, ctypes.POINTER(XINPUT_VIBRATION)]
xinput.XInputSetState.restype = wt.DWORD
ERROR_SUCCESS = 0

# 手柄按键：(配置里的名字, 掩码, 显示名)。LT/RT 不是按钮，用超出 16 位的假掩码表示
TRIG_L, TRIG_R = 0x10000, 0x20000
BUTTONS = [
    ("A", 0x1000, "A"),
    ("B", 0x2000, "B"),
    ("X", 0x4000, "X"),
    ("Y", 0x8000, "Y"),
    ("LB", 0x0100, "LB 左肩键"),
    ("RB", 0x0200, "RB 右肩键"),
    ("LT", TRIG_L, "LT 左扳机"),
    ("RT", TRIG_R, "RT 右扳机"),
    ("Back", 0x0020, "Back（两个方框）"),
    ("Start", 0x0010, "Start（三条横线）"),
    ("L3", 0x0040, "L3 按下左摇杆"),
    ("R3", 0x0080, "R3 按下右摇杆"),
    ("Up", 0x0001, "十字键 ↑"),
    ("Down", 0x0002, "十字键 ↓"),
    ("Left", 0x0004, "十字键 ←"),
    ("Right", 0x0008, "十字键 →"),
]
BUTTON_IDS = [b[0] for b in BUTTONS]
BUTTON_LABELS = {b[0]: b[2] for b in BUTTONS}

# ---------------- SendInput ----------------
ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wt.UINT

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0800, 0x1000
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP = 0x0001, 0x0002

# 鼠标键：名字 -> (按下标志, 抬起标志, mouseData)
MOUSE_BUTTONS = {
    "left": (0x0002, 0x0004, 0),
    "right": (0x0008, 0x0010, 0),
    "middle": (0x0020, 0x0040, 0),
    "x1": (0x0080, 0x0100, 1),      # 侧键·后退
    "x2": (0x0080, 0x0100, 2),      # 侧键·前进
}

# 键盘按键名 -> 虚拟键码。组合键用 + 连接，例如 ctrl+c、alt+tab、win+d
KEY_NAMES = {
    "ctrl": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B,
    "esc": 0x1B, "enter": 0x0D, "space": 0x20, "tab": 0x09,
    "backspace": 0x08, "delete": 0x2E, "insert": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "capslock": 0x14, "numlock": 0x90, "scrolllock": 0x91,
    "printscreen": 0x2C, "pausebreak": 0x13, "apps": 0x5D,
    "volumeup": 0xAF, "volumedown": 0xAE, "mute": 0xAD,
    "playpause": 0xB3, "nexttrack": 0xB0, "prevtrack": 0xB1, "mediastop": 0xB2,
    "browser_back": 0xA6, "browser_forward": 0xA7,
    "browser_refresh": 0xA8, "browser_home": 0xAC,
    ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF,
    "`": 0xC0, "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
}
KEY_NAMES.update({chr(c).lower(): c for c in range(0x41, 0x5B)})
KEY_NAMES.update({str(d): 0x30 + d for d in range(10)})
KEY_NAMES.update({f"f{n}": 0x6F + n for n in range(1, 25)})
KEY_NAMES.update({f"num{d}": 0x60 + d for d in range(10)})
KEY_ALIASES = {
    "control": "ctrl", "escape": "esc", "return": "enter", "del": "delete",
    "pgup": "pageup", "pgdn": "pagedown", "windows": "win", "menu": "apps",
    "back": "browser_back", "forward": "browser_forward",
}
EXTENDED_KEYS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2C, 0x2D, 0x2E,
                 0x5B, 0x5C, 0x5D, 0x90} | set(range(0xA6, 0xB8))
MODIFIER_VKS = {0x10, 0x11, 0x12, 0x5B}


def parse_combo(text):
    """'ctrl+shift+s' -> [0x11, 0x10, 0x53]；无效时抛 ValueError。"""
    text = text.strip().lower().replace(" ", "")
    if not text:
        raise ValueError("没有填按键")
    vks = []
    for part in text.split("+"):
        part = KEY_ALIASES.get(part, part)
        m = re.fullmatch(r"vk([0-9a-f]{2})", part)
        if m:
            vks.append(int(m.group(1), 16))
        elif part in KEY_NAMES:
            vks.append(KEY_NAMES[part])
        else:
            raise ValueError(f"不认识的按键名「{part}」")
    return vks


def _send(*inputs):
    arr = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def mouse_event(flags, dx=0, dy=0, data=0):
    _send(INPUT(INPUT_MOUSE, _INPUTUNION(
        mi=MOUSEINPUT(dx, dy, ctypes.c_uint32(data).value, flags, 0, 0))))


def key_event(vk, up=False):
    flags = KEYEVENTF_KEYUP if up else 0
    if vk in EXTENDED_KEYS:
        flags |= KEYEVENTF_EXTENDEDKEY
    _send(INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=KEYBDINPUT(vk, 0, flags, 0, 0))))


def hotkey(*vks):
    for vk in vks:
        key_event(vk)
    for vk in reversed(vks):
        key_event(vk, up=True)


# ---------------- 按键功能 ----------------
# 下拉框里的功能：(代码, 显示名)。"key" 表示键盘按键，具体按键另外填写
ACTION_CHOICES = [
    ("none", "无（空键）"),
    ("mouse:left", "鼠标左键"),
    ("mouse:right", "鼠标右键"),
    ("mouse:middle", "鼠标中键"),
    ("mouse:x1", "鼠标侧键·后退"),
    ("mouse:x2", "鼠标侧键·前进"),
    ("key", "键盘按键 / 组合键"),
    ("special:pause", "暂停 / 恢复"),
    ("special:slow", "精准慢速（按住）"),
    ("special:fast", "加速（按住）"),
    ("special:osk", "屏幕键盘"),
    ("special:overlay", "按键一览（显示/隐藏）"),
    ("special:exit", "退出 PadMouse"),
]
ACTION_LABELS = dict(ACTION_CHOICES)

# 选键用的屏幕键盘：每行是 (显示文字, 按键名, 宽度)，按键名为 None 的是空隙；宽度以一个普通键为 1
def _gap(w):
    return (None, None, w)


def _fkeys(a, b):
    return [(f"F{n}", f"f{n}", 1) for n in range(a, b + 1)]


KEYBOARD_ROWS = [
    [("Esc", "esc", 1), _gap(1)] + _fkeys(1, 4) + [_gap(0.5)] + _fkeys(5, 8) + [_gap(0.5)] +
    _fkeys(9, 12) + [_gap(0.5), ("PrtSc", "printscreen", 1), ("ScrLk", "scrolllock", 1),
                     ("Pause", "pausebreak", 1)],
    [("`", "`", 1)] + [(d, d, 1) for d in "1234567890"] +
    [("-", "-", 1), ("=", "=", 1), ("Backspace", "backspace", 2), _gap(0.5),
     ("Ins", "insert", 1), ("Home", "home", 1), ("PgUp", "pageup", 1)],
    [("Tab", "tab", 1.5)] + [(c.upper(), c, 1) for c in "qwertyuiop"] +
    [("[", "[", 1), ("]", "]", 1), ("\\", "\\", 1.5), _gap(0.5),
     ("Del", "delete", 1), ("End", "end", 1), ("PgDn", "pagedown", 1)],
    [("Caps", "capslock", 1.75)] + [(c.upper(), c, 1) for c in "asdfghjkl"] +
    [(";", ";", 1), ("'", "'", 1), ("Enter", "enter", 2.25)],
    [("Shift", "shift", 2.25)] + [(c.upper(), c, 1) for c in "zxcvbnm"] +
    [(",", ",", 1), (".", ".", 1), ("/", "/", 1), ("Shift", "shift", 2.75), _gap(1.5),
     ("↑", "up", 1)],
    [("Ctrl", "ctrl", 1.5), ("Win", "win", 1.25), ("Alt", "alt", 1.25), ("空格", "space", 6.5),
     ("Alt", "alt", 1.25), ("Menu", "apps", 1.25), ("Ctrl", "ctrl", 2), _gap(0.5),
     ("←", "left", 1), ("↓", "down", 1), ("→", "right", 1)],
]
EXTRA_KEYS = [("音量+", "volumeup"), ("音量-", "volumedown"), ("静音", "mute"),
              ("播放/暂停", "playpause"), ("上一曲", "prevtrack"), ("下一曲", "nexttrack"),
              ("浏览器后退", "browser_back"), ("浏览器前进", "browser_forward"),
              ("刷新", "browser_refresh"), ("NumLock", "numlock")]
MODIFIER_NAMES = ["ctrl", "shift", "alt", "win"]
VK_KEY_NAMES = {}                   # 虚拟键码 -> 按键名，选键时直接按键盘用
for _name, _vk in KEY_NAMES.items():
    VK_KEY_NAMES.setdefault(_vk, _name)
VK_KEY_NAMES[0x5C] = "win"          # 右 Win
KEY_DISPLAY = {name: label for row in KEYBOARD_ROWS for label, name, _ in row if name}
KEY_DISPLAY.update(dict((name, label) for label, name in EXTRA_KEYS))
KEY_DISPLAY["space"] = "Space"


def pretty_combo(text):
    """'ctrl+shift+s' -> 'Ctrl + Shift + S'，给人看的写法。"""
    parts = [KEY_ALIASES.get(p, p) for p in text.lower().replace(" ", "").split("+") if p]
    return " + ".join(KEY_DISPLAY.get(p, p.upper() if len(p) <= 3 else p) for p in parts)
STICK_CHOICES = [("move", "移动鼠标"), ("scroll", "滚轮"), ("none", "无")]
STICK_LABELS = dict(STICK_CHOICES)

# 按键一览里可以显示的项目：两个摇杆 + 所有手柄键
OVERLAY_ITEMS = [("LS", "左摇杆"), ("RS", "右摇杆")] + [(bid, label) for bid, _, label in BUTTONS]
OVERLAY_ITEM_IDS = [iid for iid, _ in OVERLAY_ITEMS]

DEFAULT_CONFIG = {
    "version": 1,
    "buttons": {
        "A": "mouse:left", "B": "mouse:right", "X": "mouse:middle",
        "Y": "special:osk",
        "LB": "key:browser_back", "RB": "key:browser_forward",
        "LT": "special:slow", "RT": "special:fast",
        "Back": "key:esc", "Start": "special:pause",
        "L3": "key:win+tab", "R3": "key:win",
        "Up": "key:up", "Down": "key:down", "Left": "key:left", "Right": "key:right",
    },
    "repeat": ["Up", "Down", "Left", "Right"],
    "left_stick": "move",
    "right_stick": "scroll",
    "mouse_speed": 1400,
    "scroll_speed": 1500,
    "overlay": {
        "show_on_start": False,     # PadMouse 启动时自动显示按键一览
        "hide_empty": True,         # 不显示空键
        "items": OVERLAY_ITEM_IDS,  # 显示哪些按键
    },
}
SPEED_RANGES = {"mouse_speed": (100, 6000), "scroll_speed": (100, 10000)}


class Action:
    def __init__(self, code, repeat=False):
        self.code = code
        self.kind, _, self.arg = code.partition(":")
        self.vks = []
        if self.kind == "key":
            self.vks = parse_combo(self.arg)
        elif self.kind == "mouse":
            if self.arg not in MOUSE_BUTTONS:
                raise ValueError(f"未知的鼠标键「{self.arg}」")
        elif self.kind == "special":
            if self.arg not in ("pause", "slow", "fast", "osk", "overlay", "exit"):
                raise ValueError(f"未知的功能「{self.arg}」")
        elif self.kind != "none":
            raise ValueError(f"未知的功能「{code}」")
        # 只有单个非修饰键才连发（避免反复按 Win 之类）
        self.repeat = (repeat and self.kind == "key"
                       and not MODIFIER_VKS.issuperset(self.vks[-1:]))
        # 按住期间保持按下、松开时才抬起的功能
        self.holdable = self.kind in ("mouse", "key") or self.arg in ("slow", "fast")

    def press(self):
        if self.kind == "mouse":
            down, _, data = MOUSE_BUTTONS[self.arg]
            mouse_event(down, data=data)
        elif self.kind == "key":
            for vk in self.vks:
                key_event(vk)
        elif self.code == "special:osk":
            hotkey(0x5B, 0x11, 0x4F)    # Win+Ctrl+O

    def release(self):
        if self.kind == "mouse":
            _, up, data = MOUSE_BUTTONS[self.arg]
            mouse_event(up, data=data)
        elif self.kind == "key":
            for vk in reversed(self.vks):
                key_event(vk, up=True)

    def describe(self):
        if self.kind == "key":
            return pretty_combo(self.arg) + ("（连发）" if self.repeat else "")
        return ACTION_LABELS.get(self.code, self.code)


def load_config():
    """读取配置；文件不存在或某项无效时用默认值补上。"""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            user = json.load(f)
    except FileNotFoundError:
        return cfg
    except (OSError, ValueError) as e:
        log(f"配置文件读取失败，使用默认按键：{e}")
        return cfg
    if not isinstance(user, dict):
        return cfg
    for bid, code in (user.get("buttons") or {}).items():
        if bid in BUTTON_IDS and isinstance(code, str):
            try:
                Action(code)
                cfg["buttons"][bid] = code
            except ValueError as e:
                log(f"{bid} 的设置无效，使用默认：{e}")
    if isinstance(user.get("repeat"), list):
        cfg["repeat"] = [b for b in user["repeat"] if b in BUTTON_IDS]
    for key in ("left_stick", "right_stick"):
        if user.get(key) in STICK_LABELS:
            cfg[key] = user[key]
    for key, (lo, hi) in SPEED_RANGES.items():
        value = user.get(key)
        if isinstance(value, (int, float)) and lo <= value <= hi:
            cfg[key] = int(value)
    overlay = user.get("overlay")
    if isinstance(overlay, dict):
        for key in ("show_on_start", "hide_empty"):
            if isinstance(overlay.get(key), bool):
                cfg["overlay"][key] = overlay[key]
        if isinstance(overlay.get("items"), list):
            cfg["overlay"]["items"] = [i for i in OVERLAY_ITEM_IDS if i in overlay["items"]]
    return cfg


def save_config(cfg):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def config_mtime():
    try:
        return os.path.getmtime(CONFIG_PATH)
    except OSError:
        return None


class Mapping:
    """把配置编译成主循环直接用的结构。"""
    def __init__(self, cfg):
        repeat = set(cfg["repeat"])
        self.buttons = [(bid, mask, Action(cfg["buttons"][bid], bid in repeat))
                        for bid, mask, _ in BUTTONS]
        self.left_stick = cfg["left_stick"]
        self.right_stick = cfg["right_stick"]
        self.mouse_speed = cfg["mouse_speed"]
        self.scroll_speed = cfg["scroll_speed"]


def overlay_entries(cfg):
    """按键一览要画的行：(项目, "stick", 摇杆模式) 或 (项目, "action", Action)，按设置筛选。"""
    m = Mapping(cfg)
    shown, hide_empty = set(cfg["overlay"]["items"]), cfg["overlay"]["hide_empty"]
    entries = []
    for iid, mode in (("LS", m.left_stick), ("RS", m.right_stick)):
        if iid in shown and not (hide_empty and mode == "none"):
            entries.append((iid, "stick", mode))
    for bid, _, action in m.buttons:
        if bid in shown and not (hide_empty and action.kind == "none"):
            entries.append((bid, "action", action))
    return entries


# ---------------- 工具函数 ----------------
def stick(x, y, deadzone):
    """把摇杆读数转成 (-1..1, -1..1)，带圆形死区和曲线。"""
    mag = (x * x + y * y) ** 0.5
    if mag <= deadzone:
        return 0.0, 0.0
    norm = min((mag - deadzone) / (32767 - deadzone), 1.0)
    curved = norm ** MOUSE_CURVE
    return x / mag * curved, y / mag * curved


def find_controller():
    state = XINPUT_STATE()
    for i in range(4):
        if xinput.XInputGetState(i, ctypes.byref(state)) == ERROR_SUCCESS:
            return i
    return None


class Vibrator:
    def __init__(self):
        self.until = 0.0

    def buzz(self, idx, seconds=0.15, strength=30000):
        if not VIBRATE_FEEDBACK:
            return
        vib = XINPUT_VIBRATION(strength, strength)
        xinput.XInputSetState(idx, ctypes.byref(vib))
        self.until = time.perf_counter() + seconds

    def update(self, idx):
        if self.until and time.perf_counter() >= self.until:
            vib = XINPUT_VIBRATION(0, 0)
            xinput.XInputSetState(idx, ctypes.byref(vib))
            self.until = 0.0


MB_FRONT = 0x10000 | 0x40000   # MB_SETFOREGROUND | MB_TOPMOST：提示框总在最前


def log(text):
    """写一行运行日志，出问题时可以查看。"""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text.rstrip() + "\n")
    except OSError:
        pass


def message_box(text, title="PadMouse", flags=0x40):
    """在后台线程弹出提示框，不阻塞托盘。"""
    threading.Thread(target=user32.MessageBoxW,
                     args=(None, text, title, flags | MB_FRONT), daemon=True).start()


def main_running():
    """托盘主程序是否在运行（看它创建的单实例互斥量还在不在）。"""
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.OpenMutexW.restype = wt.HANDLE
    handle = kernel32.OpenMutexW(0x00100000, False, INSTANCE_MUTEX)     # SYNCHRONIZE
    if handle:
        kernel32.CloseHandle(handle)
    return bool(handle)


def settings_in_front():
    """改键设置窗口（或它弹出的选键窗口）是不是当前的前台窗口。"""
    hwnd = user32.FindWindowW(None, SETTINGS_TITLE)
    fg = user32.GetForegroundWindow()
    if not hwnd or not fg:
        return False
    a, b = wt.DWORD(), wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(a))
    user32.GetWindowThreadProcessId(fg, ctypes.byref(b))
    return a.value == b.value


def launch_self(flag):
    """用同一个程序再开一个窗口进程（改键设置 / 按键一览）。"""
    if getattr(sys, "frozen", False):
        args = [sys.executable, flag]
    else:
        args = [sys.executable, os.path.abspath(__file__), flag]
    try:
        subprocess.Popen(args)
    except OSError as e:
        message_box(f"打开窗口失败：{e}", flags=0x10)


def fatal(text):
    """记录错误并弹出提示框，等用户关掉后再返回（程序随后退出）。"""
    log(text)
    user32.MessageBoxW(None, f"{text}\n\n日志：{LOG_PATH}", "PadMouse 出错", 0x10 | MB_FRONT)


# ---------------- 托盘图标 ----------------
COLOR_ON, COLOR_OFF, COLOR_NONE = (46, 204, 113), (150, 150, 150), (231, 76, 60)


def make_icon(color):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 14, 62, 50), radius=16, fill=color)
    d.ellipse((12, 23, 24, 35), fill="white")             # 左摇杆
    d.ellipse((38, 31, 48, 41), fill="white")             # 右摇杆
    d.ellipse((46, 21, 52, 27), fill="white")             # 按键
    d.rectangle((26, 36, 34, 39), fill="white")           # 十字键
    d.rectangle((28, 33, 31, 42), fill="white")
    return img


class App:
    def __init__(self):
        self.enabled = True
        self.connected = False
        self.running = True
        self.icon = None
        self.cfg = load_config()
        self.images = {c: make_icon(c) for c in (COLOR_ON, COLOR_OFF, COLOR_NONE)}

    def refresh(self):
        if not self.icon:
            return
        if not self.connected:
            color, tip = COLOR_NONE, "PadMouse：等待手柄连接"
        elif self.enabled:
            color, tip = COLOR_ON, "PadMouse：运行中"
        else:
            color, tip = COLOR_OFF, "PadMouse：已暂停"
        # 只改图标和提示文字；菜单是固定的，不在后台线程里重建，避免托盘卡死
        self.icon.icon = self.images[color]
        self.icon.title = tip

    def notify(self, text, title="PadMouse"):
        try:
            self.icon.notify(text, title)
        except Exception:
            log("通知失败：\n" + traceback.format_exc())

    def toggle(self, *_):
        self.enabled = not self.enabled
        self.refresh()

    def open_settings(self, *_):
        launch_self("--settings")

    def toggle_overlay(self, *_):
        toggle_overlay_window()

    def stop(self, *_):
        self.running = False
        if self.icon:
            self.icon.stop()


# ---------------- 手柄主循环（后台线程） ----------------
def gamepad_loop(app):
    winmm = ctypes.WinDLL("winmm")
    winmm.timeBeginPeriod(1)  # 让 sleep 更精确

    idx = None
    prev = 0
    was_enabled = app.enabled
    active = {}                  # 正按住的键：bid -> [Action, 下一次连发时间或 None]
    acc_x = acc_y = 0.0          # 亚像素累积
    acc_sx = acc_sy = 0.0        # 滚轮累积
    vib = Vibrator()
    state = XINPUT_STATE()
    mapping = Mapping(app.cfg)
    cfg_mtime = config_mtime()
    next_cfg_check = 0.0
    captured, next_focus_check = False, 0.0     # 手柄是否让给了改键设置窗口
    last = time.perf_counter()
    period = 1.0 / POLL_HZ

    def release_all():
        for action, _ in active.values():
            action.release()
        active.clear()

    try:
        while app.running:
            # 配置文件改了就重新加载（改键窗口保存后 1 秒内生效）
            now = time.perf_counter()
            if now >= next_cfg_check:
                next_cfg_check = now + 1.0
                mtime = config_mtime()
                if mtime != cfg_mtime:
                    cfg_mtime = mtime
                    release_all()
                    app.cfg = load_config()
                    mapping = Mapping(app.cfg)
                    log("按键配置已重新加载")
                    app.notify("新的按键设置已生效。")

            if idx is None:
                idx = find_controller()
                if idx is None:
                    time.sleep(1)
                    last = time.perf_counter()
                    continue
                app.connected = True
                app.refresh()
                prev = 0

            if xinput.XInputGetState(idx, ctypes.byref(state)) != ERROR_SUCCESS:
                release_all()
                idx = None
                app.connected = False
                app.refresh()
                continue

            now = time.perf_counter()
            dt = min(now - last, 0.05)
            last = now
            pad = state.Gamepad
            btns = pad.wButtons
            if pad.bLeftTrigger > TRIGGER_THRESHOLD:
                btns |= TRIG_L
            if pad.bRightTrigger > TRIGGER_THRESHOLD:
                btns |= TRIG_R
            pressed = btns & ~prev
            released = prev & ~btns
            prev = btns

            # 改键设置在最前面时把手柄让给它（按手柄键在页面上选中按键），不控制鼠标键盘
            if now >= next_focus_check:
                next_focus_check = now + 0.2
                if settings_in_front() != captured:
                    captured = not captured
                    if captured:
                        release_all()
                        acc_x = acc_y = acc_sx = acc_sy = 0.0
            if captured:
                time.sleep(max(0.0, period - (time.perf_counter() - now)))
                continue

            # 「暂停/恢复」在暂停状态下也要响应
            for _, mask, action in mapping.buttons:
                if pressed & mask and action.code == "special:pause":
                    app.toggle()

            # 状态变化（手柄或托盘点击）：释放按键并震动提示
            if app.enabled != was_enabled:
                was_enabled = app.enabled
                if not app.enabled:
                    release_all()
                vib.buzz(idx, 0.1 if app.enabled else 0.3)

            vib.update(idx)

            if app.enabled:
                # 按键：按下 / 松开
                for bid, mask, action in mapping.buttons:
                    if pressed & mask:
                        if action.code == "special:exit":
                            app.running = False
                            break
                        if action.code == "special:overlay":
                            app.toggle_overlay()
                            continue
                        action.press()
                        if action.holdable:
                            active[bid] = [action, now + REPEAT_DELAY if action.repeat else None]
                    elif released & mask and bid in active:
                        active.pop(bid)[0].release()
                if not app.running:
                    break

                # 按住连发：重复发送最后一个键
                for entry in active.values():
                    action, due = entry
                    if due is not None and now >= due:
                        key_event(action.vks[-1])
                        entry[1] = now + REPEAT_RATE

                # 速度倍率
                factor = 1.0
                for action, _ in active.values():
                    if action.code == "special:slow":
                        factor *= SLOW_FACTOR
                    elif action.code == "special:fast":
                        factor *= FAST_FACTOR

                # 摇杆：移动鼠标 / 滚轮
                mx = my = sx = sy = 0.0
                for x, y, dz, mode in ((pad.sThumbLX, pad.sThumbLY, DEADZONE_LEFT, mapping.left_stick),
                                       (pad.sThumbRX, pad.sThumbRY, DEADZONE_RIGHT, mapping.right_stick)):
                    if mode == "none":
                        continue
                    vx, vy = stick(x, y, dz)
                    if mode == "move":
                        mx += vx
                        my += vy
                    else:
                        sx += vx
                        sy += vy

                acc_x += mx * mapping.mouse_speed * factor * dt
                acc_y -= my * mapping.mouse_speed * factor * dt
                ix, iy = int(acc_x), int(acc_y)
                if ix or iy:
                    mouse_event(MOUSEEVENTF_MOVE, ix, iy)
                    acc_x -= ix
                    acc_y -= iy
                if not (mx or my):
                    acc_x = acc_y = 0.0

                acc_sy += sy * mapping.scroll_speed * dt
                acc_sx += sx * mapping.scroll_speed * dt
                if abs(acc_sy) >= 30:
                    mouse_event(MOUSEEVENTF_WHEEL, data=int(acc_sy))
                    acc_sy -= int(acc_sy)
                if abs(acc_sx) >= 30:
                    mouse_event(MOUSEEVENTF_HWHEEL, data=int(acc_sx))
                    acc_sx -= int(acc_sx)
                if not sy:
                    acc_sy = 0.0
                if not sx:
                    acc_sx = 0.0

            time.sleep(max(0.0, period - (time.perf_counter() - now)))
    except Exception:
        fatal("手柄线程出错退出：\n\n" + traceback.format_exc())
    finally:
        release_all()
        if idx is not None:
            xinput.XInputSetState(idx, ctypes.byref(XINPUT_VIBRATION(0, 0)))
        winmm.timeEndPeriod(1)
        log("手柄线程结束")
        app.stop()


def run():
    # 防止重复启动（重复运行会让鼠标移动双倍）
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wt.HANDLE
    _mutex = kernel32.CreateMutexW(None, False, INSTANCE_MUTEX)
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        log("已有实例在运行，本次启动退出")
        user32.MessageBoxW(
            None,
            "PadMouse 已经在运行了。\n\n"
            "图标在任务栏右下角的 ^（显示隐藏的图标）里。\n"
            "如果那里也找不到，打开任务管理器结束 PadMouse 或 pythonw 后再启动。",
            "PadMouse", 0x40 | MB_FRONT)
        return

    import pystray
    app = App()
    menu = pystray.Menu(
        pystray.MenuItem("暂停 / 恢复", app.toggle, default=True),
        pystray.MenuItem("按键一览", app.toggle_overlay),
        pystray.MenuItem("改键设置…", app.open_settings),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", app.stop),
    )
    app.icon = pystray.Icon("PadMouse", app.images[COLOR_NONE],
                            "PadMouse：等待手柄连接", menu)

    def setup(icon):
        icon.visible = True
        log("启动")
        app.notify("图标在任务栏右下角，被收起时点 ^ 查看。\n"
                   "右键图标可以改键。", "PadMouse 已启动")
        if app.cfg["overlay"]["show_on_start"] and not user32.FindWindowW(None, OVERLAY_TITLE):
            launch_self("--overlay")
        threading.Thread(target=gamepad_loop, args=(app,), daemon=True).start()

    app.icon.run(setup=setup)
    # 按键一览和改键设置都是单独的进程，退出时一起关掉（它们自己也会发现主程序没了而退出）
    for title in (OVERLAY_TITLE, SETTINGS_TITLE):
        hwnd = user32.FindWindowW(None, title)
        if hwnd:
            user32.PostMessageW(hwnd, 0x0010, 0, 0)    # WM_CLOSE
    log("退出")


# ---------------- 改键设置窗口 ----------------
def run_settings():
    import base64
    import io
    import tkinter as tk
    from tkinter import ttk, messagebox

    # 已经开着就切到前台
    hwnd = user32.FindWindowW(None, SETTINGS_TITLE)
    if hwnd:
        user32.ShowWindow(hwnd, 9)          # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(1)   # 高分屏下字不发虚
    except (OSError, AttributeError):
        pass

    try:
        ctypes.WinDLL("imm32").ImmDisableIME(0xFFFFFFFF)     # 关掉中文输入法，不然键盘输入会进候选框
    except (OSError, AttributeError):
        pass

    cfg = load_config()
    root = tk.Tk()
    root.title(SETTINGS_TITLE)
    root.resizable(False, False)
    root.option_add("*Font", ("Microsoft YaHei UI", 10))
    try:
        buf = io.BytesIO()
        make_icon(COLOR_ON).save(buf, "PNG")
        root.iconphoto(True, tk.PhotoImage(data=base64.b64encode(buf.getvalue())))
    except Exception:
        pass

    outer = ttk.Frame(root, padding=16)
    outer.grid(sticky="nsew")
    notebook = ttk.Notebook(outer)
    notebook.grid(row=0, column=0, sticky="nsew")
    frm = ttk.Frame(notebook, padding=14)           # 第一页：按键设置
    notebook.add(frm, text="  按键设置  ")
    ov_page = ttk.Frame(notebook, padding=14)       # 第二页：按键一览
    notebook.add(ov_page, text="  按键一览  ")
    hint = ("点卡片里的一行（或手柄图上的按键）选中它，在下面改功能；这个窗口在最前面时，"
            "手柄不控制鼠标，按手柄上的键会直接选中它。\n"
            "键盘按键：点「选择…」，在弹出的键盘上点选或直接按键盘上的键，"
            "Ctrl / Shift / Alt / Win 可以多选组成组合键。")
    ttk.Label(frm, text=hint, foreground="#555", justify="left").grid(
        row=0, column=0, sticky="w", pady=(0, 10))

    action_values = [label for _, label in ACTION_CHOICES]
    action_code = {label: code for code, label in ACTION_CHOICES}
    stick_values = [label for _, label in STICK_CHOICES]
    stick_code = {label: code for code, label in STICK_CHOICES}
    rows = {bid: (tk.StringVar(), tk.StringVar(), tk.BooleanVar()) for bid in BUTTON_IDS}
    left_var, right_var = tk.StringVar(), tk.StringVar()
    mouse_var, scroll_var = tk.StringVar(), tk.StringVar()
    stick_vars = {"LS": left_var, "RS": right_var}
    dpad_var, dpad_rep = tk.StringVar(), tk.BooleanVar()       # 十字键整组的模式和连发
    dpad_code = {label: code for code, label in DPAD_MODES}
    dpad_label = dict(DPAD_MODES)

    # 手柄图画布：高分屏按 DPI 放大，但不超出屏幕高度
    dpi = root.winfo_fpixels("1i") / 96
    scale = max(0.8, min(dpi, (root.winfo_screenheight() - 300 * dpi) / BIND_H))
    kit = _draw_kit(scale)
    canvas = tk.Canvas(frm, width=round(BIND_W * scale), height=round(BIND_H * scale),
                       bg=PAGE_BG, highlightthickness=0, bd=0)
    canvas.grid(row=1, column=0)

    # 下面的编辑区：选中哪一行就编辑哪一行
    editor = ttk.Frame(frm, padding=(2, 12, 0, 0))
    editor.grid(row=2, column=0, sticky="w")
    sel_title = ttk.Label(editor, font=("Microsoft YaHei UI", 12, "bold"), width=15)
    sel_title.grid(row=0, column=0, sticky="w", padx=(0, 10))
    btn_box = ttk.Frame(editor)
    btn_box.grid(row=0, column=1, sticky="w")
    act_cb = ttk.Combobox(btn_box, values=action_values, state="readonly", width=18)
    act_cb.pack(side="left", padx=(0, 8))
    key_ent = ttk.Entry(btn_box, width=20)
    key_ent.pack(side="left", padx=(0, 6))
    pick = ttk.Button(btn_box, text="选择…", width=8, command=lambda: pick_keys(sel["id"]))
    pick.pack(side="left", padx=(0, 10))
    rep_chk = ttk.Checkbutton(btn_box, text="按住连发")
    rep_chk.pack(side="left")
    stick_box = ttk.Frame(editor)
    stick_box.grid(row=0, column=1, sticky="w")
    stick_cb = ttk.Combobox(stick_box, values=stick_values, state="readonly", width=12)
    stick_cb.pack(side="left", padx=(0, 20))
    for text, var, key in (("鼠标速度", mouse_var, "mouse_speed"), ("滚动速度", scroll_var, "scroll_speed")):
        lo, hi = SPEED_RANGES[key]
        ttk.Label(stick_box, text=text).pack(side="left", padx=(0, 6))
        ttk.Spinbox(stick_box, textvariable=var, from_=lo, to=hi, increment=100,
                    width=8).pack(side="left", padx=(0, 16))
    ttk.Label(stick_box, text="（两个摇杆共用）", foreground="#888").pack(side="left")
    dpad_box = ttk.Frame(editor)
    dpad_box.grid(row=0, column=1, sticky="w")
    ttk.Label(dpad_box, text="十字键作为").pack(side="left", padx=(0, 8))
    dpad_cb = ttk.Combobox(dpad_box, textvariable=dpad_var, values=[l for _, l in DPAD_MODES],
                           state="readonly", width=18)
    dpad_cb.pack(side="left", padx=(0, 10))
    dpad_rep_chk = ttk.Checkbutton(dpad_box, text="按住连发", variable=dpad_rep)
    dpad_tip = ttk.Label(dpad_box, text="（在上面的十字键卡片里分别点四个方向来设置）", foreground="#888")

    sel = {"id": "A"}
    view = {"custom": False, "popup": False}     # 十字键是否分开显示；选键弹窗是否开着

    def sync(bid):
        if bid != sel["id"] or bid not in rows:
            return
        state = "normal" if action_code[rows[bid][0].get()] == "key" else "disabled"
        for w in (key_ent, pick, rep_chk):
            w.configure(state=state)

    act_cb.bind("<<ComboboxSelected>>", lambda e: sync(sel["id"]))

    def pick_keys(bid):
        """弹出屏幕键盘选键（也可以直接按键盘上的键），确定后写回这一行的输入框。"""
        keytext = rows[bid][1]
        parts = [KEY_ALIASES.get(p, p) for p in keytext.get().lower().replace(" ", "").split("+") if p]
        mods = {p for p in parts if p in MODIFIER_NAMES}
        mains = [p for p in parts if p not in MODIFIER_NAMES]
        chosen = {"main": mains[-1] if mains else None}

        top = tk.Toplevel(root)
        top.title(f"选择按键 — {BUTTON_LABELS[bid]}")
        top.resizable(False, False)
        top.transient(root)
        view["popup"] = True
        top.bind("<Destroy>", lambda e: view.update(popup=False) if e.widget is top else None)
        unit = max(34, round(root.winfo_fpixels("1i") * 0.42))   # 一个普通键的边长（像素）
        gap = max(3, unit // 12)
        bg_key, bg_on, bg_hover = "#ffffff", "#2563eb", "#e3ecfd"
        body = tk.Frame(top, padx=16, pady=14)
        body.pack()
        tk.Label(body, text="点一个键，或直接按键盘上的键；Ctrl / Shift / Alt / Win 可以多选，"
                            "和其他键组成组合键。再点一次取消。",
                 fg="#555").pack(anchor="w", pady=(0, 10))
        key_buttons = {}        # 按键名 -> [tk.Button]（Shift/Ctrl/Alt 左右各一个）

        def current():
            main = [chosen["main"]] if chosen["main"] else []
            return "+".join([m for m in MODIFIER_NAMES if m in mods] + main)

        def paint():
            for name, btns in key_buttons.items():
                on = name in mods or name == chosen["main"]
                for b in btns:
                    b.configure(bg=bg_on if on else bg_key, fg="white" if on else "#111",
                                activebackground="#1d4ed8" if on else bg_hover,
                                activeforeground="white" if on else "#111")
            combo = current()
            preview.configure(text=pretty_combo(combo) if combo else "（还没选）")
            ok.configure(state="normal" if combo else "disabled")

        def click(name):
            if name in MODIFIER_NAMES:
                mods.symmetric_difference_update({name})
            else:
                chosen["main"] = None if chosen["main"] == name else name
            paint()

        def on_key(e):
            # 直接按键盘：Windows 上 keycode 就是虚拟键码。修饰键切换选中，其他键直接选上
            name = VK_KEY_NAMES.get(e.keycode)
            if name in MODIFIER_NAMES:
                click(name)
            elif name:
                chosen["main"] = name
                paint()
            return "break"          # 不让 Tab / 空格 / Esc 等触发窗口自己的操作

        def add_key(parent, label, name, w):
            cell = tk.Frame(parent, width=round(w * (unit + gap)) - gap, height=unit,
                            bg="#c3c9d2")      # 这层底色就是按键的边框
            cell.pack_propagate(False)
            cell.pack(side="left", padx=(0, gap))
            b = tk.Button(cell, text=label, relief="flat", bd=0, cursor="hand2", takefocus=0,
                          font=("Microsoft YaHei UI", 9), command=lambda: click(name))
            b.pack(fill="both", expand=True, padx=1, pady=1)
            key_buttons.setdefault(name, []).append(b)

        kb = tk.Frame(body)
        kb.pack(anchor="w")
        for i, row in enumerate(KEYBOARD_ROWS):
            line = tk.Frame(kb)
            line.pack(anchor="w", pady=(0, gap * 3 if i == 0 else gap))
            for label, name, w in row:
                if name is None:
                    tk.Frame(line, width=round(w * (unit + gap)), height=1).pack(side="left")
                else:
                    add_key(line, label, name, w)

        tk.Label(body, text="多媒体 / 浏览器", fg="#888").pack(anchor="w", pady=(10, 4))
        for chunk in (EXTRA_KEYS[:5], EXTRA_KEYS[5:]):
            line = tk.Frame(body)
            line.pack(anchor="w", pady=(0, gap))
            for label, name in chunk:
                add_key(line, label, name, 2.5)

        bottom = tk.Frame(body)
        bottom.pack(fill="x", pady=(14, 0))
        tk.Label(bottom, text="已选：", fg="#555").pack(side="left")
        preview = tk.Label(bottom, font=("Microsoft YaHei UI", 11, "bold"))
        preview.pack(side="left")

        def clear():
            mods.clear()
            chosen["main"] = None
            paint()

        def confirm():
            keytext.set(current())
            top.destroy()

        ok = ttk.Button(bottom, text="确定", takefocus=0, command=confirm)
        ok.pack(side="right", padx=(6, 0))
        ttk.Button(bottom, text="取消", takefocus=0, command=top.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(bottom, text="清除", takefocus=0, command=clear).pack(side="right")
        top.bind("<KeyPress>", on_key)
        paint()

        # 摆在设置窗口正中，并且不超出屏幕
        top.update_idletasks()
        x = root.winfo_rootx() + (root.winfo_width() - top.winfo_width()) // 2
        y = root.winfo_rooty() + (root.winfo_height() - top.winfo_height()) // 3
        top.geometry(f"+{max(0, x)}+{max(0, y)}")
        top.grab_set()
        top.focus_set()

    def to_photo(img):
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return tk.PhotoImage(data=base64.b64encode(buf.getvalue()))

    def button_visual(bid):
        """一行右边的功能图：鼠标图标、键帽或文字。"""
        choice, keytext, rep = rows[bid]
        code = action_code.get(choice.get(), "none")
        if code == "key":
            text = keytext.get().strip().lower().replace(" ", "")
            try:
                return kit.action_visual("action", Action("key:" + text, rep.get()))
            except ValueError:
                return kit.text("（按键无效）" if text else "（还没选按键）", fill=PAD_DIM)
        return kit.action_visual("action", Action(code))

    def visual(iid):
        if iid in stick_vars:
            return kit.action_visual("stick", stick_code[stick_vars[iid].get()])
        if iid == "Dpad":
            mode = dpad_code.get(dpad_var.get(), "custom")
            if mode in DPAD_KEYS:
                parts = [kit.keycap(KEY_DISPLAY.get(k, k.upper())) for k in DPAD_KEYS[mode]]
                if dpad_rep.get():
                    parts.append(kit.text("连发", kit.small, "#9ca3af"))
                return kit.hstack(parts, kit.S(4))
            if mode == "none":
                return kit.text("—", fill="#9ca3af")
            return kit.text("分别设置 ↓", fill=PAD_DIM)
        return button_visual(iid)

    photos, items, layout = {}, {}, {}      # items: 行 -> (画布图片, 宽)
    hover = {"id": None}

    def draw_row(iid):
        if iid not in items:
            return
        item, w = items[iid]
        state = "selected" if iid == sel["id"] else "hover" if iid == hover["id"] else "normal"
        photos[iid] = to_photo(render_bind_row(kit, iid, visual(iid), w, state))
        canvas.itemconfigure(item, image=photos[iid])

    def draw_pad():
        photos["pad"] = to_photo(render_bind_page(kit, sel["id"], layout["cards"]))
        canvas.itemconfigure(layout["pad"], image=photos["pad"])

    def set_hover(iid):
        old, hover["id"] = hover["id"], iid
        canvas.configure(cursor="hand2" if iid else "")
        for i in {old, iid} - {None}:
            draw_row(i)

    def pad_motion(e):
        canvas.configure(cursor="hand2" if pad_hit(e.x / scale, e.y / scale) else "")

    def pad_click(e):
        iid = pad_hit(e.x / scale, e.y / scale)
        if iid:
            select(iid)

    def relayout(custom):
        """十字键在整组 / 分开之间切换时，重新摆放卡片。"""
        view["custom"] = custom
        canvas.delete("all")
        items.clear()
        hover["id"] = None
        layout["cards"], spots = bind_layout(custom)
        layout["pad"] = canvas.create_image(0, 0, anchor="nw")
        canvas.tag_bind(layout["pad"], "<Motion>", pad_motion)
        canvas.tag_bind(layout["pad"], "<Button-1>", pad_click)
        for iid, (x, y, w) in spots.items():
            item = canvas.create_image(round(x * scale), round(y * scale), anchor="nw")
            items[iid] = (item, w)
            canvas.tag_bind(item, "<Enter>", lambda e, i=iid: set_hover(i))
            canvas.tag_bind(item, "<Leave>", lambda e: set_hover(None))
            canvas.tag_bind(item, "<Button-1>", lambda e, i=iid: select(i))
        if sel["id"] not in items:
            sel["id"] = "Dpad"
        draw_pad()
        for iid in items:
            draw_row(iid)

    def show_dpad_editor():
        custom = dpad_code.get(dpad_var.get()) == "custom"
        dpad_rep_chk.pack_forget()
        dpad_tip.pack_forget()
        (dpad_tip if custom else dpad_rep_chk).pack(side="left")

    def select(iid):
        if iid in DPAD_DIRS and not view["custom"]:
            iid = "Dpad"                    # 整组模式下，按哪个方向都选中整组
        old, sel["id"] = sel["id"], iid
        for i in {old, iid}:
            draw_row(i)
        draw_pad()
        sel_title.configure(text=BIND_NAMES[iid])
        for box in (btn_box, stick_box, dpad_box):
            box.grid_remove()
        if iid in stick_vars:
            stick_box.grid()
            stick_cb.configure(textvariable=stick_vars[iid])
        elif iid == "Dpad":
            dpad_box.grid()
            show_dpad_editor()
        else:
            btn_box.grid()
            choice, keytext, rep = rows[iid]
            act_cb.configure(textvariable=choice)
            key_ent.configure(textvariable=keytext)
            rep_chk.configure(variable=rep)
            sync(iid)

    def apply_dpad_mode(*_):
        """选了十字键模式：预设就把四个方向一起设好；选「分别设置」就把四个方向分开显示。"""
        mode = dpad_code[dpad_var.get()]
        if mode in DPAD_KEYS:
            for d, key in zip(DPAD_DIRS, DPAD_KEYS[mode]):
                choice, keytext, rep = rows[d]
                choice.set(ACTION_LABELS["key"])
                keytext.set(key)
                rep.set(dpad_rep.get())
        elif mode == "none":
            for d in DPAD_DIRS:
                rows[d][0].set(ACTION_LABELS["none"])
                rows[d][1].set("")
        if (mode == "custom") != view["custom"]:
            relayout(mode == "custom")
        select("Dpad")

    def dpad_rep_changed(*_):
        if dpad_code.get(dpad_var.get()) in DPAD_KEYS:
            for d in DPAD_DIRS:
                rows[d][2].set(dpad_rep.get())

    dpad_cb.bind("<<ComboboxSelected>>", apply_dpad_mode)
    dpad_rep.trace_add("write", dpad_rep_changed)

    def load_dpad():
        """按四个方向现在的设置决定十字键显示成整组还是分开。"""
        codes = {}
        for d in DPAD_DIRS:
            choice, keytext, _ = rows[d]
            code = action_code[choice.get()]
            codes[d] = "key:" + keytext.get().strip().lower() if code == "key" else code
        mode = dpad_mode(codes)
        dpad_var.set(dpad_label[mode])
        dpad_rep.set(all(rows[d][2].get() for d in DPAD_DIRS))
        relayout(mode == "custom")

    # 改了功能就重画对应的行（连续输入时合并成一次）
    dirty = set()

    def redraw_dirty():
        for i in list(dirty):
            draw_row(i)
        dirty.clear()

    def mark(*ids):
        if not dirty:
            root.after(40, redraw_dirty)
        dirty.update(ids)

    for iid, vars_ in rows.items():
        for var in vars_:
            var.trace_add("write", lambda *_, i=iid: mark(i, "Dpad") if i in DPAD_DIRS else mark(i))
    for iid, var in stick_vars.items():
        var.trace_add("write", lambda *_, i=iid: mark(i))
    for var in (dpad_var, dpad_rep):
        var.trace_add("write", lambda *_: mark("Dpad"))

    # 窗口在最前面时读手柄：按下哪个键就选中哪一行，推动摇杆选中摇杆（PadMouse 这时不会控制鼠标）
    pad = {"idx": None, "prev": 0, "next_find": 0.0, "sticks": set()}

    def poll_pad():
        try:
            now = time.monotonic()
            if pad["idx"] is None and now >= pad["next_find"]:
                pad["idx"], pad["next_find"] = find_controller(), now + 1.0
            st = XINPUT_STATE()
            if pad["idx"] is not None:
                if xinput.XInputGetState(pad["idx"], ctypes.byref(st)) != ERROR_SUCCESS:
                    pad["idx"] = None
                else:
                    g = st.Gamepad
                    btns = g.wButtons
                    if g.bLeftTrigger > TRIGGER_THRESHOLD:
                        btns |= TRIG_L
                    if g.bRightTrigger > TRIGGER_THRESHOLD:
                        btns |= TRIG_R
                    pressed, pad["prev"] = btns & ~pad["prev"], btns
                    moved = {sid for sid, x, y in (("LS", g.sThumbLX, g.sThumbLY),
                                                    ("RS", g.sThumbRX, g.sThumbRY))
                             if x * x + y * y > 24000 ** 2}
                    new_sticks, pad["sticks"] = moved - pad["sticks"], moved
                    if settings_in_front() and not view["popup"]:
                        hit = [bid for bid, mask, _ in BUTTONS if pressed & mask] + sorted(new_sticks)
                        if hit:
                            select(hit[0])
        except Exception:
            log("改键页读取手柄出错：\n" + traceback.format_exc())
        root.after(30, poll_pad)

    # ---- 第二页：按键一览 ----
    show_start_var, hide_empty_var = tk.BooleanVar(), tk.BooleanVar()
    item_vars = {iid: tk.BooleanVar() for iid in OVERLAY_ITEM_IDS}
    ov_left = ttk.Frame(ov_page)
    ov_left.grid(row=0, column=0, sticky="nw")
    ttk.Label(ov_left, text="按键一览显示在屏幕右下角：透明背景，用图标标出每个手柄键的功能，\n"
                            "鼠标可以直接点穿它。用托盘菜单「按键一览」、下面的按钮，\n"
                            "或设了「按键一览」功能的手柄键来显示 / 隐藏。",
              foreground="#555", justify="left").grid(row=0, column=0, sticky="w", pady=(0, 12))
    ttk.Checkbutton(ov_left, text="PadMouse 启动时自动显示", variable=show_start_var).grid(
        row=1, column=0, sticky="w", pady=2)
    ttk.Checkbutton(ov_left, text="不显示空键", variable=hide_empty_var).grid(
        row=2, column=0, sticky="w", pady=2)
    ttk.Label(ov_left, text="显示哪些按键：").grid(row=3, column=0, sticky="w", pady=(14, 4))
    items_box = ttk.Frame(ov_left)
    items_box.grid(row=4, column=0, sticky="w")
    for i, (iid, label) in enumerate(OVERLAY_ITEMS):
        ttk.Checkbutton(items_box, text=label, variable=item_vars[iid]).grid(
            row=i // 3, column=i % 3, sticky="w", padx=(0, 22), pady=2)
    select_row = ttk.Frame(ov_left)
    select_row.grid(row=5, column=0, sticky="w", pady=(8, 0))
    ttk.Button(select_row, text="全选",
               command=lambda: [v.set(True) for v in item_vars.values()]).pack(side="left")
    ttk.Button(select_row, text="全不选",
               command=lambda: [v.set(False) for v in item_vars.values()]).pack(side="left", padx=6)
    ttk.Button(ov_left, text="现在显示 / 隐藏按键一览", command=toggle_overlay_window).grid(
        row=6, column=0, sticky="w", pady=(18, 0))
    ttk.Label(ov_left, text="（显示的是已保存的设置，改完先点「保存」）", foreground="#888").grid(
        row=7, column=0, sticky="w", pady=(4, 0))

    # 右边：实时预览（深色底，模拟桌面）
    preview_box = tk.Frame(ov_page, bg="#2d3440", padx=14, pady=14)
    preview_box.grid(row=0, column=1, sticky="n", padx=(24, 0))
    tk.Label(preview_box, text="预览", bg="#2d3440", fg="#9aa4b2").pack(anchor="w")
    preview = tk.Label(preview_box, bg="#2d3440")
    preview.pack(anchor="w", pady=(6, 0))
    preview_state = {"photo": None, "pending": False}

    def update_preview():
        preview_state["pending"] = False
        if notebook.index("current") != 1:
            return
        new, _ = collect(strict=False)
        try:
            scale = root.winfo_fpixels("1i") / 96 * 0.7     # 预览缩小一点，放得下全部按键
            img = render_overlay(new, scale)
            buf = io.BytesIO()
            img.save(buf, "PNG")
            preview_state["photo"] = tk.PhotoImage(data=base64.b64encode(buf.getvalue()))
            preview.configure(image=preview_state["photo"])
        except Exception:
            log("预览失败：\n" + traceback.format_exc())

    def schedule_preview(*_):
        if not preview_state["pending"]:            # 连续点选时合并成一次重画
            preview_state["pending"] = True
            root.after(80, update_preview)

    for var in [show_start_var, hide_empty_var, *item_vars.values()]:
        var.trace_add("write", schedule_preview)
    notebook.bind("<<NotebookTabChanged>>", schedule_preview)

    def fill(c):
        repeat = set(c["repeat"])
        for bid, (choice, keytext, rep, *_) in rows.items():
            code = c["buttons"][bid]
            if code.startswith("key:"):
                choice.set(ACTION_LABELS["key"])
                keytext.set(code[4:])
            else:
                choice.set(ACTION_LABELS.get(code, ACTION_LABELS["none"]))
                keytext.set("")
            rep.set(bid in repeat)
            sync(bid)
        left_var.set(STICK_LABELS[c["left_stick"]])
        right_var.set(STICK_LABELS[c["right_stick"]])
        mouse_var.set(str(c["mouse_speed"]))
        scroll_var.set(str(c["scroll_speed"]))
        show_start_var.set(c["overlay"]["show_on_start"])
        hide_empty_var.set(c["overlay"]["hide_empty"])
        shown = set(c["overlay"]["items"])
        for iid, var in item_vars.items():
            var.set(iid in shown)
        load_dpad()
        select(sel["id"])

    def collect(strict=True):
        """把窗口里的设置收集成配置。strict 时遇到无效内容返回错误信息；否则跳过（给预览用）。"""
        new = {"version": 1, "buttons": {}, "repeat": []}
        for bid, (choice, keytext, rep, *_) in rows.items():
            code = action_code[choice.get()]
            if code == "key":
                text = keytext.get().strip().lower().replace(" ", "")
                try:
                    parse_combo(text)
                except ValueError as e:
                    if strict:
                        return None, f"「{BUTTON_LABELS[bid]}」的键盘按键无效：{e}"
                    text = None
                code = "key:" + text if text else "none"
                if text and rep.get():
                    new["repeat"].append(bid)
            new["buttons"][bid] = code
        new["left_stick"] = stick_code[left_var.get()]
        new["right_stick"] = stick_code[right_var.get()]
        for key, var, text in (("mouse_speed", mouse_var, "鼠标速度"),
                               ("scroll_speed", scroll_var, "滚动速度")):
            lo, hi = SPEED_RANGES[key]
            try:
                value = int(float(var.get()))
            except ValueError:
                value = None
            if value is None or not lo <= value <= hi:
                if strict:
                    return None, f"{text}要在 {lo}～{hi} 之间。"
                value = DEFAULT_CONFIG[key]
            new[key] = value
        new["overlay"] = {"show_on_start": show_start_var.get(), "hide_empty": hide_empty_var.get(),
                          "items": [iid for iid in OVERLAY_ITEM_IDS if item_vars[iid].get()]}
        return new, None

    def save():
        new, error = collect()
        if error:
            messagebox.showerror(SETTINGS_TITLE, error, parent=root)
            return
        try:
            save_config(new)
        except OSError as e:
            messagebox.showerror(SETTINGS_TITLE, f"保存失败：{e}", parent=root)
            return
        log("改键设置已保存")
        messagebox.showinfo(SETTINGS_TITLE, "已保存。PadMouse 正在运行的话，新按键会在 1 秒内生效。",
                            parent=root)
        root.destroy()

    btns = ttk.Frame(outer)
    btns.grid(row=1, column=0, sticky="e", pady=(14, 0))
    ttk.Button(btns, text="恢复默认", command=lambda: fill(DEFAULT_CONFIG)).pack(side="left", padx=4)
    ttk.Button(btns, text="取消", command=root.destroy).pack(side="left", padx=4)
    ttk.Button(btns, text="保存", command=save).pack(side="left", padx=4)

    # PadMouse 退出（或被结束进程）后这个窗口也跟着关掉；单独打开的改键设置不受影响
    watch = {"main_seen": main_running()}

    def watch_main():
        if main_running():
            watch["main_seen"] = True
        elif watch["main_seen"]:
            log("PadMouse 已退出，关闭改键设置")
            root.destroy()
            return
        root.after(1000, watch_main)

    fill(cfg)
    root.after(1000, watch_main)
    root.after(30, poll_pad)
    root.mainloop()


# ---------------- 按键一览：绘制 ----------------
PAD_FACE = {"A": "#3fb950", "B": "#f85149", "X": "#4493f8", "Y": "#e3b341"}
GLYPH_INK, ICON_EDGE, INK, ACCENT = "#0d131b", "#d1d5db", "#f9fafb", "#60a5fa"
MOUSE_TEXT = {"left": "左键", "right": "右键", "middle": "中键",
              "x1": "侧键 · 后退", "x2": "侧键 · 前进"}


def _font(size, bold=False):
    from PIL import ImageFont
    names = (["msyhbd.ttc", "segoeuib.ttf", "arialbd.ttf"] if bold
             else ["msyh.ttc", "segoeui.ttf", "arial.ttf"])
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def _draw_kit(scale=1.0):
    """按键一览和改键页共用的绘图工具：手柄键图标、功能图标等，都先画 3 倍大再缩小。"""
    from types import SimpleNamespace
    from PIL import Image, ImageChops, ImageDraw

    ss = 3                                   # 先画 3 倍大再缩小，边缘更平滑
    k = scale * ss

    def S(v):
        return max(1, round(v * k))

    H = S(28)                                # 每行图标的高度
    font, bold, small = _font(S(14)), _font(S(13), True), _font(S(11), True)
    big = _font(S(17), True)

    def blank(w):
        img = Image.new("RGBA", (max(1, w), H), (0, 0, 0, 0))
        return img, ImageDraw.Draw(img)

    def text(s, fnt=font, fill=INK):
        w = round(fnt.getlength(s)) + S(2)
        img, d = blank(w)
        d.text((0, H / 2), s, font=fnt, fill=fill, anchor="lm")
        return img

    def hstack(parts, gap=S(6)):
        w = sum(p.width for p in parts) + gap * (len(parts) - 1)
        img, _ = blank(w)
        x = 0
        for p in parts:
            img.alpha_composite(p, (x, 0))
            x += p.width + gap
        return img

    # ---- 手柄键图标（白色实心、深色字，和 Xbox 设置里的样子一样）----
    def round_badge(label, face):
        img, d = blank(H)
        d.ellipse((S(1), S(1), H - 1 - S(1), H - 1 - S(1)), fill=face)
        d.text((H / 2, H / 2 + S(0.5)), label, font=bold, fill=GLYPH_INK, anchor="mm")
        return img

    def bumper(label):                       # 肩键：白色胶囊
        w = S(36)
        img, d = blank(w)
        d.rounded_rectangle((0, S(5), w - 1, H - S(5)), radius=S(9), fill=INK)
        d.text((w / 2, H / 2), label, font=small, fill=GLYPH_INK, anchor="mm")
        return img

    def trigger(label, left):                # 扳机：外侧上角是大圆角，其余是小圆角
        w = S(30)
        img = blank(w)[0]
        mask = Image.new("L", img.size, 0)
        md = ImageDraw.Draw(mask)
        top, r = S(1), S(12)
        md.rounded_rectangle((0, top, w - 1, H - 1 - S(1)), radius=S(4), fill=255)
        x0 = 0 if left else w - 1 - 2 * r
        md.rectangle((x0, top, x0 + 2 * r, top + r), fill=0)
        md.pieslice((x0, top, x0 + 2 * r, top + 2 * r), 180 if left else 270,
                    270 if left else 360, fill=255)
        md.rectangle((x0 + (r if left else 0), top, x0 + (2 * r if left else r), top + 2 * r), fill=255)
        img.paste(INK, mask=mask)
        ImageDraw.Draw(img).text((w / 2, H / 2 + S(2)), label, font=small, fill=GLYPH_INK, anchor="mm")
        return img

    def stick_badge(label, arrow=False):     # 侧面看的摇杆：上面一条帽子，下面是杆身；点击带个向下的箭头
        img, d = blank(S(30))
        c = img.width / 2
        if arrow:
            d.polygon([(c - S(4), 0), (c + S(4), 0), (c, S(5))], fill=INK)
        d.rounded_rectangle((c - S(14), S(7), c + S(14), S(12)), radius=S(2), fill=INK)
        d.rounded_rectangle((c - S(8), S(11), c + S(8), H - 1), radius=S(3), fill=INK)
        d.text((c, S(19.5)), label, font=small, fill=GLYPH_INK, anchor="mm")
        return img

    def view_badge():                        # 「选择」：两个叠在一起的方框
        img, d = blank(H)
        u, w = S(7), S(2)
        d.rectangle((S(4), S(5), S(4) + 2 * u, S(5) + 2 * u), outline=INK, width=w)
        d.rectangle((S(10), S(11), S(10) + 2 * u, S(11) + 2 * u), fill=(0, 0, 0, 0), outline=INK, width=w)
        return img

    def menu_badge():                        # 「开始」：三条横线
        img, d = blank(H)
        for y in (S(7), H / 2, H - S(7)):
            d.line((S(3), y, H - S(3), y), fill=INK, width=S(2.2))
        return img

    def dpad(direction):                     # 白色十字，按下的那一臂末端有个指向外的缺口
        img, d = blank(H)
        c, arm, half = H / 2, S(9) / 2, H / 2 - S(1)
        d.rectangle((c - arm, c - half, c + arm, c + half), fill=INK)
        d.rectangle((c - half, c - arm, c + half, c + arm), fill=INK)
        if direction is None:                # 整个十字键
            return img
        t, e = S(3.2), half - S(1.5)
        dx, dy = {"Up": (0, -1), "Down": (0, 1), "Left": (-1, 0), "Right": (1, 0)}[direction]
        tip = (c + dx * e, c + dy * e)
        base = (c + dx * (e - t * 1.6), c + dy * (e - t * 1.6))
        d.polygon([tip, (base[0] + dy * t, base[1] + dx * t), (base[0] - dy * t, base[1] - dx * t)],
                  fill=(0, 0, 0, 0))
        return img

    def pad_icon(iid):
        if iid in PAD_FACE:
            return round_badge(iid, PAD_FACE[iid])
        if iid in ("LB", "RB"):
            return bumper(iid)
        if iid in ("LT", "RT"):
            return trigger(iid, iid == "LT")
        if iid in ("LS", "RS", "L3", "R3"):
            return stick_badge(iid[0], arrow=iid[1] == "3")
        if iid == "Back":
            return view_badge()
        if iid == "Start":
            return menu_badge()
        return dpad(None if iid == "Dpad" else iid)

    # ---- 功能 ----
    def mouse(part=None):
        w = S(19)
        img, d = blank(w)
        body = (S(1), S(1), w - 1 - S(1), H - 1 - S(1))
        d.rounded_rectangle(body, radius=S(8), fill="#374151")
        mid, split = w / 2, S(11)
        if part in ("left", "right"):
            mask = Image.new("L", img.size, 0)
            ImageDraw.Draw(mask).rounded_rectangle(body, radius=S(8), fill=255)
            region = Image.new("L", img.size, 0)
            x0, x1 = (0, mid) if part == "left" else (mid, w)
            ImageDraw.Draw(region).rectangle((x0, 0, x1, split), fill=255)
            img.paste(ACCENT, mask=ImageChops.multiply(mask, region))
        d.line((S(2), split, w - S(2), split), fill=ICON_EDGE, width=S(1.5))
        d.line((mid, S(2), mid, split), fill=ICON_EDGE, width=S(1.5))
        wheel = (mid - S(2), S(4), mid + S(2), S(9))
        d.rounded_rectangle(wheel, radius=S(2), fill=ACCENT if part in ("middle", "wheel") else ICON_EDGE)
        d.rounded_rectangle(body, radius=S(8), outline=INK, width=S(2))
        if part in ("x1", "x2"):             # 侧键：画在外框上面，凸出来
            d.rounded_rectangle((0, S(13), S(5), S(21)), radius=S(2), fill=ACCENT,
                                outline=INK, width=S(1))
        return img

    def keycap(label):
        w = max(S(26), round(small.getlength(label)) + S(14))
        img, d = blank(w)
        d.rounded_rectangle((0, S(3), w - 1, H - 1), radius=S(6), fill="#9ca3af")       # 键帽侧面
        d.rounded_rectangle((0, S(1), w - 1, H - S(4)), radius=S(6), fill="#f9fafb",
                            outline="#d1d5db", width=S(1))
        d.text((w / 2, (S(1) + H - S(4)) / 2), label, font=small, fill="#111827", anchor="mm")
        return img

    def action_visual(kind, value):
        if kind == "stick":
            if value == "none":
                return text("—", fill="#9ca3af")
            return hstack([mouse(None if value == "move" else "wheel"), text(STICK_LABELS[value])])
        action = value
        if action.kind == "none":
            return text("—", fill="#9ca3af")
        if action.kind == "mouse":
            return hstack([mouse(action.arg), text(MOUSE_TEXT[action.arg])])
        if action.kind == "key":
            parts = []
            for name in action.arg.split("+"):
                if parts:
                    parts.append(text("+", fill="#e5e7eb"))
                name = KEY_ALIASES.get(name, name)
                parts.append(keycap(KEY_DISPLAY.get(name, name.upper() if len(name) <= 3 else name)))
            if action.repeat:
                parts.append(text("连发", small, "#9ca3af"))
            return hstack(parts, S(4))
        return text(ACTION_LABELS.get(action.code, action.code))

    def shrink(img):
        # 缩小时按预乘 alpha 处理，避免透明边缘发黑
        return img.convert("RGBa").resize((img.width // ss, img.height // ss),
                                          Image.LANCZOS).convert("RGBA")

    return SimpleNamespace(S=S, H=H, k=k, font=font, bold=bold, small=small, big=big,
                           blank=blank, text=text, hstack=hstack, stick_badge=stick_badge,
                           keycap=keycap, pad_icon=pad_icon, action_visual=action_visual,
                           shrink=shrink)


def render_overlay(cfg, scale=1.0):
    """把按键一览画成一张透明底的 RGBA 图：左边是手柄键图标，右边是功能。"""
    from PIL import Image, ImageFilter

    kit = _draw_kit(scale)
    S, H = kit.S, kit.H
    rows = [(kit.pad_icon(iid), kit.action_visual(kind, value))
            for iid, kind, value in overlay_entries(cfg)]
    if not rows:
        rows = [(kit.stick_badge("?"), kit.text("按键一览：没有要显示的按键"))]

    # ---- 排版：图标一列居中对齐，功能左对齐 ----
    icon_w = max(icon.width for icon, _ in rows)
    gap, row_gap, pad = S(14), S(7), S(10)
    width = pad * 2 + icon_w + gap + max(vis.width for _, vis in rows)
    height = pad * 2 + len(rows) * H + (len(rows) - 1) * row_gap
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    for i, (icon, vis) in enumerate(rows):
        y = pad + i * (H + row_gap)
        layer.alpha_composite(icon, (pad + (icon_w - icon.width) // 2, y))
        layer.alpha_composite(vis, (pad + icon_w + gap, y))

    # 柔和的黑色阴影，让白字在浅色背景上也看得清
    alpha = layer.getchannel("A").filter(ImageFilter.GaussianBlur(S(2.5)))
    alpha = alpha.point(lambda a: min(255, a * 2))
    shadow = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    shadow.putalpha(alpha)
    out = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    out.alpha_composite(shadow, (0, S(1)))
    out.alpha_composite(layer)
    return kit.shrink(out)


# ---------------- 改键页：中间是手柄，四周是 Steam 输入那样的分组卡片 ----------------
# 布局单位是 96 DPI 下的像素。手柄轮廓坐标是在 1768 宽的参考图上量的，画时缩放、平移到页面中间
BIND_W, BIND_H, BIND_ROW, CARD_HEAD = 1000, 466, 34, 30
PAD_OX, PAD_OY, PAD_K = 309, 22, 0.45
PAGE_BG, CARD_BG, CARD_EDGE = "#0d131b", "#151c26", "#273140"
PAD_LINE, PAD_DIM, SEL_FILL = "#5b6573", "#8b96a5", "#1e3a5f"
ROW_BG = {"normal": CARD_BG, "hover": "#1c2532", "selected": "#26364d"}

# 十字键整组设置：(代码, 显示名)。预设模式下四个方向作为一整行显示
DPAD_DIRS = ("Up", "Down", "Left", "Right")
DPAD_KEYS = {"arrows": ("up", "down", "left", "right"), "wasd": ("w", "s", "a", "d")}
DPAD_MODES = [("arrows", "方向键（↑ ↓ ← →）"), ("wasd", "WASD"),
              ("custom", "分别设置四个方向"), ("none", "无（空键）")]

# 卡片：(名字, 标题, 放在哪一列, 卡片里的行, 引线指向的手柄位置（参考图坐标）)
BIND_CARDS = [
    ("lshoulder", "左扳机 / 左肩键", "left", ["LT", "LB"], (672, 100)),
    ("lstick", "左摇杆", "left", ["LS", "L3"], (630, 253)),
    ("dpad", "十字键", "left", ["Dpad", *DPAD_DIRS], (726, 392)),
    ("rshoulder", "右扳机 / 右肩键", "right", ["RT", "RB"], (1150, 100)),
    ("face", "正面按键", "right", ["A", "B", "X", "Y"], (1221, 255)),
    ("rstick", "右摇杆", "right", ["RS", "R3"], (1087, 383)),
    ("menu", "菜单键", "center", ["Back", "Start"], None),
]
BIND_NAMES = dict(BUTTON_LABELS, LS="左摇杆", RS="右摇杆", Dpad="十字键")


def _mirror(pts):
    return [(1823 - x, y) for x, y in pts]          # 1823 = 左右对称轴 × 2


def _pad_xy(x, y):
    """参考图坐标 -> 页面坐标。"""
    return PAD_OX + (x - 488) * PAD_K, PAD_OY + (y - 60) * PAD_K


# 左半边轮廓：从徽标下方顺时针绕到底部正中，右半边镜像得到
_BODY_LEFT = [(911, 213), (860, 212), (815, 195), (780, 150), (755, 125), (700, 118), (650, 135),
              (617, 158), (580, 215), (545, 320), (515, 430), (492, 540), (495, 620), (530, 668),
              (585, 685), (625, 665), (690, 565), (745, 522), (800, 517), (911, 517)]
PAD_BODY = _BODY_LEFT + _mirror(_BODY_LEFT[-2:0:-1])
PAD_BUMPER = [(800, 107), (765, 94), (720, 95), (668, 108), (632, 128), (617, 158)]
PAD_TRIGGER = [(652, 112), (664, 84), (700, 66), (745, 64), (768, 94)]
DPAD_C, DPAD_ARM, DPAD_LEN = (797, 392), 31, 61
# 点手柄图选键用的圆形热区：(圆心 x, 圆心 y, 半径)，多个热区重叠时取最小的
PAD_SPOTS = {
    "A": (1133, 312, 30), "B": (1191, 255, 30), "X": (1073, 255, 30), "Y": (1133, 197, 30),
    "Back": (849, 253, 22), "Start": (973, 253, 22),
    "L3": (692, 253, 34), "LS": (692, 253, 62), "R3": (1025, 383, 34), "RS": (1025, 383, 62),
    "Up": (797, 358, 30), "Down": (797, 426, 30), "Left": (763, 392, 30), "Right": (831, 392, 30),
    "LB": (705, 102, 40), "RB": (1118, 102, 40), "LT": (712, 72, 30), "RT": (1111, 72, 30),
}


def dpad_mode(codes):
    """从四个方向的设置推出十字键模式：arrows / wasd / none / custom。"""
    for mode, keys in DPAD_KEYS.items():
        if all(codes[d] == "key:" + k for d, k in zip(DPAD_DIRS, keys)):
            return mode
    if all(codes[d] == "none" for d in DPAD_DIRS):
        return "none"
    return "custom"


def bind_layout(dpad_custom):
    """卡片和每一行的位置。返回 (cards, rows)：
    cards = [(名字, 标题, x, y, 宽, 高, 引线起点, 引线终点, 行)]，rows = {行: (x, y, 宽)}。"""
    cards, rows = [], {}
    next_y = {"left": 10, "right": 10}
    for name, title, col, ids, anchor in BIND_CARDS:
        if name == "dpad" and not dpad_custom:
            ids = ["Dpad"]
        h = CARD_HEAD + len(ids) * BIND_ROW + 8
        if col == "center":
            x, y, w = 350, 320, 300
        else:
            x, y, w = (10 if col == "left" else 720), next_y[col], 270
            next_y[col] += h + 12
        start = end = None
        if anchor:
            start = (x + w if col == "left" else x, y + CARD_HEAD + BIND_ROW / 2)
            end = _pad_xy(*anchor)
        cards.append((name, title, x, y, w, h, start, end, ids))
        for i, iid in enumerate(ids):
            rows[iid] = (x + 6, y + CARD_HEAD + i * BIND_ROW, w - 12)
    return cards, rows


def pad_hit(x, y):
    """页面坐标 -> 手柄图上点到的按键（没点到返回 None）。"""
    rx, ry = (x - PAD_OX) / PAD_K + 488, (y - PAD_OY) / PAD_K + 60
    hits = [(r, iid) for iid, (cx, cy, r) in PAD_SPOTS.items()
            if (rx - cx) ** 2 + (ry - cy) ** 2 <= r * r]
    return min(hits)[1] if hits else None


def _spline(pts, closed=False, steps=10):
    """Catmull-Rom 曲线：把一串控制点连成平滑的线。"""
    ext = [pts[-1]] + pts + pts[:2] if closed else [pts[0]] + pts + [pts[-1]]
    out = []
    for i in range(len(ext) - 3):
        p0, p1, p2, p3 = ext[i:i + 4]
        for s in range(steps):
            t = s / steps
            out.append(tuple(0.5 * (2 * b + (c - a) * t + (2 * a - 5 * b + 4 * c - e) * t * t
                                    + (3 * b - a - 3 * c + e) * t ** 3)
                             for a, b, c, e in zip(p0, p1, p2, p3)))
    out.append(ext[-2])
    return out


def render_bind_page(kit, selected, cards):
    """改键页的底图：手柄轮廓（选中的按键高亮）、卡片底板、标题和引线。"""
    from PIL import Image, ImageDraw

    S, k = kit.S, kit.k
    img = Image.new("RGBA", (S(BIND_W), S(BIND_H)), PAGE_BG)
    d = ImageDraw.Draw(img)

    def P(x, y):
        px, py = _pad_xy(x, y)
        return px * k, py * k

    def pen(*ids):
        return (ACCENT, S(3)) if selected in ids else (PAD_LINE, S(2))

    def curve(pts, *ids, closed=False):
        color, w = pen(*ids)
        d.line(_spline([P(*p) for p in pts], closed), fill=color, width=w, joint="curve")

    def circle(x, y, r, *ids, letter=None):
        color, w = pen(*ids)
        (cx, cy), rr = P(x, y), r * PAD_K * k
        d.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), outline=color, width=w,
                  fill=SEL_FILL if selected in ids else None)
        if letter:
            d.text((cx, cy), letter, font=kit.big, fill=color, anchor="mm")

    # ---- 卡片和引线 ----
    for name, title, x, y, w, h, start, end, ids in cards:
        on = selected in ids
        if start:
            color = ACCENT if on else CARD_EDGE
            d.line([(start[0] * k, start[1] * k), (end[0] * k, end[1] * k)], fill=color, width=S(1.5))
            ex, ey = end[0] * k, end[1] * k
            d.ellipse((ex - S(3), ey - S(3), ex + S(3), ey + S(3)), fill=color)
        d.rounded_rectangle((S(x), S(y), S(x + w), S(y + h)), radius=S(8), fill=CARD_BG,
                            outline=ACCENT if on else CARD_EDGE, width=S(1.5))
        d.text((S(x + 12), S(y + CARD_HEAD / 2 + 1)), title, font=kit.bold,
               fill=INK if on else PAD_DIM, anchor="lm")

    # ---- 手柄 ----
    curve(PAD_BODY, closed=True)
    curve(PAD_BUMPER, "LB")
    curve(_mirror(PAD_BUMPER), "RB")
    d.line([P(*PAD_BUMPER[0]), P(*_mirror(PAD_BUMPER)[0])], fill=PAD_LINE, width=S(2))
    curve(PAD_TRIGGER, "LT")
    curve(_mirror(PAD_TRIGGER), "RT")
    circle(911, 168, 30)                     # 徽标键
    circle(849, 253, 19, "Back")
    circle(973, 253, 19, "Start")
    for (x, y), side in (((692, 253), "L"), ((1025, 383), "R")):
        circle(x, y, 62, side + "S")
        circle(x, y, 50, side + "S")
        circle(x, y, 34, side + "3")
    for iid in ("A", "B", "X", "Y"):
        x, y, _ = PAD_SPOTS[iid]
        circle(x, y, 30, iid, letter=iid)

    # 十字键：外圈 + 十字轮廓，选中的方向（整组时是四个方向）填色
    (cx, cy), a, n = DPAD_C, DPAD_ARM, DPAD_LEN
    circle(cx, cy, 71)
    arms = {"Up": (cx - a, cy - n, cx + a, cy - a), "Down": (cx - a, cy + a, cx + a, cy + n),
            "Left": (cx - n, cy - a, cx - a, cy + a), "Right": (cx + a, cy - a, cx + n, cy + a)}
    lit = list(arms) if selected == "Dpad" else [selected] if selected in arms else []
    for arm in lit:
        x0, y0, x1, y1 = arms[arm]
        d.rectangle((*P(x0, y0), *P(x1, y1)), fill=SEL_FILL)
    if selected == "Dpad":
        d.rectangle((*P(cx - a, cy - a), *P(cx + a, cy + a)), fill=SEL_FILL)
    cross = [(cx - a, cy - n), (cx + a, cy - n), (cx + a, cy - a), (cx + n, cy - a), (cx + n, cy + a),
             (cx + a, cy + a), (cx + a, cy + n), (cx - a, cy + n), (cx - a, cy + a), (cx - n, cy + a),
             (cx - n, cy - a), (cx - a, cy - a), (cx - a, cy - n)]
    d.line([P(*p) for p in cross], fill=ACCENT if selected == "Dpad" else PAD_LINE,
           width=S(3) if selected == "Dpad" else S(2), joint="curve")
    if selected in arms:
        x0, y0, x1, y1 = arms[selected]
        d.rectangle((*P(x0, y0), *P(x1, y1)), outline=ACCENT, width=S(3))
    return kit.shrink(img)


def render_bind_row(kit, iid, visual, width, state):
    """卡片里的一行：左边手柄键图标，右边绑定的功能；state 是 normal / hover / selected。"""
    from PIL import Image

    S = kit.S
    img = Image.new("RGBA", (S(width), S(BIND_ROW)), ROW_BG[state])
    if state == "selected":
        img.paste(ACCENT, (0, S(4), S(3), img.height - S(4)))
    icon = kit.pad_icon(iid)
    box = S(36)                                         # 图标统一占这么宽，功能才能对齐
    y = (img.height - kit.H) // 2
    img.alpha_composite(icon, (S(10) + (box - icon.width) // 2, y))
    x = S(10) + box + S(12)
    room = img.width - x - S(8)
    if visual.width > room:                             # 太长就截掉，完整内容在下面的编辑区
        visual = visual.crop((0, 0, room, visual.height))
    img.alpha_composite(visual, (x, y))
    return kit.shrink(img)


def toggle_overlay_window():
    hwnd = user32.FindWindowW(None, OVERLAY_TITLE)
    if hwnd:
        user32.PostMessageW(hwnd, 0x0010, 0, 0)     # WM_CLOSE
    else:
        launch_self("--overlay")


# ---------------- 按键一览小窗（屏幕右下角，透明、鼠标可点穿） ----------------
def run_overlay():
    from PIL import Image, ImageChops

    if user32.FindWindowW(None, OVERLAY_TITLE):     # 已经开着
        return
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(1)
    except (OSError, AttributeError):
        pass
    try:
        scale = user32.GetDpiForSystem() / 96
    except AttributeError:
        scale = 1.0

    gdi32 = ctypes.WinDLL("gdi32")
    kernel32 = ctypes.WinDLL("kernel32")
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                    ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                    ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                    ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                    ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                    ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]

    class BLENDFUNCTION(ctypes.Structure):
        _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                    ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]

    user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU,
                                       wt.HINSTANCE, wt.LPVOID]
    user32.CreateWindowExW.restype = wt.HWND
    user32.UpdateLayeredWindow.argtypes = [wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT),
                                           ctypes.POINTER(wt.SIZE), wt.HDC, ctypes.POINTER(wt.POINT),
                                           wt.DWORD, ctypes.POINTER(BLENDFUNCTION), wt.DWORD]
    user32.GetDC.argtypes = [wt.HWND]
    user32.GetDC.restype = wt.HDC
    user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
    user32.SetTimer.argtypes = [wt.HWND, ctypes.c_size_t, wt.UINT, ctypes.c_void_p]
    gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
    gdi32.CreateCompatibleDC.restype = wt.HDC
    gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.c_void_p, wt.UINT,
                                       ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD]
    gdi32.CreateDIBSection.restype = wt.HBITMAP
    gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
    gdi32.SelectObject.restype = wt.HGDIOBJ
    gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wt.HDC]
    kernel32.GetModuleHandleW.restype = wt.HMODULE

    state = {"mtime": object(), "img": None, "hwnd": None, "main_seen": main_running()}
    margin = round(16 * scale)

    def show(img):
        """把图贴到分层窗口上，并放在任务栏上方的右下角。"""
        w, h = img.size
        r, g, b, a = img.split()
        bgra = Image.merge("RGBA", (ImageChops.multiply(b, a), ImageChops.multiply(g, a),
                                    ImageChops.multiply(r, a), a)).tobytes()   # 预乘 alpha 的 BGRA
        area = wt.RECT()
        user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(area), 0)       # SPI_GETWORKAREA
        pos = wt.POINT(area.right - w - margin, area.bottom - h - margin)
        screen = user32.GetDC(None)
        mem = gdi32.CreateCompatibleDC(screen)
        header = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        bitmap = gdi32.CreateDIBSection(screen, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
        ctypes.memmove(bits, bgra, len(bgra))
        old = gdi32.SelectObject(mem, bitmap)
        user32.UpdateLayeredWindow(state["hwnd"], screen, ctypes.byref(pos), ctypes.byref(wt.SIZE(w, h)),
                                   mem, ctypes.byref(wt.POINT(0, 0)), 0,
                                   ctypes.byref(BLENDFUNCTION(0, 0, 255, 1)), 2)   # ULW_ALPHA
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(None, screen)

    def refresh():
        mtime = config_mtime()
        if mtime != state["mtime"] or state["img"] is None:     # 改键保存后自动重画
            state["mtime"] = mtime
            state["img"] = render_overlay(load_config(), scale)
        show(state["img"])                                       # 任务栏移动了也能跟着

    @WNDPROC
    def wndproc(hwnd, msg, wparam, lparam):
        try:
            if msg == 0x0113:                    # WM_TIMER
                # PadMouse 退出（或被结束进程）后自己关掉；单独打开的按键一览不受影响
                if main_running():
                    state["main_seen"] = True
                elif state["main_seen"]:
                    user32.DestroyWindow(hwnd)
                    return 0
                refresh()
                return 0
            if msg == 0x0010:                    # WM_CLOSE
                user32.DestroyWindow(hwnd)
                return 0
            if msg == 0x0002:                    # WM_DESTROY
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            log("按键一览出错：\n" + traceback.format_exc())
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    hinst = kernel32.GetModuleHandleW(None)
    wc = WNDCLASSW()
    wc.lpfnWndProc = wndproc
    wc.hInstance = hinst
    wc.lpszClassName = "PadMouseOverlay"
    user32.RegisterClassW(ctypes.byref(wc))
    # 分层 | 鼠标点穿 | 置顶 | 不进任务栏 | 不抢焦点
    ex_style = 0x00080000 | 0x00000020 | 0x00000008 | 0x00000080 | 0x08000000
    state["hwnd"] = user32.CreateWindowExW(ex_style, "PadMouseOverlay", OVERLAY_TITLE,
                                           0x80000000, 0, 0, 0, 0, None, None, hinst, None)   # WS_POPUP
    refresh()
    user32.ShowWindow(state["hwnd"], 4)          # SW_SHOWNOACTIVATE
    user32.SetTimer(state["hwnd"], 1, 1000, None)
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


def check():
    idx = find_controller()
    if idx is None:
        print("未检测到 XInput 手柄。请确认手柄已连接，且处于 Xbox/XInput 模式。")
        return 1
    print(f"检测到手柄 #{idx + 1}，可以使用。")
    return 0


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(check())
    try:
        if "--settings" in sys.argv:
            run_settings()
        elif "--overlay" in sys.argv:
            run_overlay()
        else:
            run()
    except Exception:
        fatal("启动失败：\n\n" + traceback.format_exc())
