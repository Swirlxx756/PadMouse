"""
PadMouse —— 用手柄代替鼠标（Windows 托盘程序）

支持 XInput 手柄：Xbox 手柄、以及切到 XInput/Xbox 模式的大多数第三方手柄。
（PS 手柄可借助 DS4Windows / Steam 转成 XInput 后使用。）
依赖：pip install pystray pillow

运行：  pythonw pad_mouse.py              以右下角托盘图标运行（无窗口）
        pythonw pad_mouse.py --settings   打开改键设置窗口
        python pad_mouse.py --check       只检测手柄是否连接

托盘图标：绿色 = 运行中，灰色 = 已暂停，红色 = 没有手柄。
左键单击图标 = 暂停/恢复；右键菜单：改键设置、按键说明、退出。

改键：托盘右键 →「改键设置…」。每个键都可以设成鼠标键、键盘按键或组合键、
暂停、慢速/加速等，也可以设成「无（空键）」让它什么都不做。
保存后立即生效，设置存放在 %LOCALAPPDATA%\\PadMouse\\config.json。

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
VK_TO_NAME = {}
for _name, _vk in KEY_NAMES.items():
    VK_TO_NAME.setdefault(_vk, _name)
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
    ("special:exit", "退出 PadMouse"),
]
ACTION_LABELS = dict(ACTION_CHOICES)
STICK_CHOICES = [("move", "移动鼠标"), ("scroll", "滚轮"), ("none", "无")]
STICK_LABELS = dict(STICK_CHOICES)

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
            if self.arg not in ("pause", "slow", "fast", "osk", "exit"):
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
            return "键盘 " + self.arg + ("（按住连发）" if self.repeat else "")
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


def describe_config(cfg):
    m = Mapping(cfg)
    lines = [f"左摇杆：{STICK_LABELS[m.left_stick]}",
             f"右摇杆：{STICK_LABELS[m.right_stick]}"]
    lines += [f"{BUTTON_LABELS[bid]}：{action.describe()}" for bid, _, action in m.buttons]
    return "\n".join(lines)


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
        if getattr(sys, "frozen", False):
            args = [sys.executable, "--settings"]
        else:
            args = [sys.executable, os.path.abspath(__file__), "--settings"]
        try:
            subprocess.Popen(args)
        except OSError as e:
            message_box(f"打开改键设置失败：{e}", flags=0x10)

    def show_help(self, *_):
        message_box(describe_config(self.cfg) +
                    "\n\n改键：托盘右键 →「改键设置…」", "PadMouse 当前按键")

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
    _mutex = kernel32.CreateMutexW(None, False, "Local\\PadMouse_SingleInstance")
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
        pystray.MenuItem("改键设置…", app.open_settings),
        pystray.MenuItem("当前按键", app.show_help),
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
        threading.Thread(target=gamepad_loop, args=(app,), daemon=True).start()

    app.icon.run(setup=setup)
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

    frm = ttk.Frame(root, padding=16)
    frm.grid(sticky="nsew")
    hint = ("每个键可以设成鼠标键、键盘按键或其他功能；选「无（空键）」就是这个键什么都不做。\n"
            "键盘按键：点「录入」后在键盘上按一下（可带 Ctrl/Shift/Alt），或者直接输入，\n"
            "组合键用 + 连接，例如 ctrl+c、alt+tab、win+d（Win 组合键会被系统拦截，请手动输入）。")
    ttk.Label(frm, text=hint, foreground="#555", justify="left").grid(
        row=0, column=0, columnspan=5, sticky="w", pady=(0, 12))

    for col, text in enumerate(("手柄按键", "功能", "键盘按键", "", "")):
        ttk.Label(frm, text=text, foreground="#888").grid(row=1, column=col, sticky="w", padx=(0, 8))

    action_values = [label for _, label in ACTION_CHOICES]
    action_code = {label: code for code, label in ACTION_CHOICES}
    rows = {}
    recording = {"bid": None, "mods": []}

    def sync(bid):
        choice, keytext, rep, ent, rec, chk = rows[bid]
        state = "normal" if action_code[choice.get()] == "key" else "disabled"
        for w in (ent, rec, chk):
            w.configure(state=state)

    def stop_record():
        if recording["bid"]:
            rows[recording["bid"]][4].configure(text="录入")
        recording["bid"] = None
        recording["mods"] = []

    def start_record(bid):
        was = recording["bid"]
        stop_record()
        if was == bid:          # 再点一次 = 取消录入
            return
        recording["bid"] = bid
        rows[bid][4].configure(text="请按键…")
        root.focus_set()

    for i, (bid, _, label) in enumerate(BUTTONS):
        r = i + 2
        choice = tk.StringVar()
        keytext = tk.StringVar()
        rep = tk.BooleanVar()
        ttk.Label(frm, text=label).grid(row=r, column=0, sticky="w", padx=(0, 12), pady=2)
        cb = ttk.Combobox(frm, textvariable=choice, values=action_values, state="readonly", width=18)
        cb.grid(row=r, column=1, sticky="w", padx=(0, 8))
        cb.bind("<<ComboboxSelected>>", lambda e, b=bid: sync(b))
        ent = ttk.Entry(frm, textvariable=keytext, width=18)
        ent.grid(row=r, column=2, sticky="w", padx=(0, 6))
        rec = ttk.Button(frm, text="录入", width=8, command=lambda b=bid: start_record(b))
        rec.grid(row=r, column=3, sticky="w", padx=(0, 8))
        chk = ttk.Checkbutton(frm, text="按住连发", variable=rep)
        chk.grid(row=r, column=4, sticky="w")
        rows[bid] = (choice, keytext, rep, ent, rec, chk)

    # 摇杆与速度
    r = len(BUTTONS) + 2
    ttk.Separator(frm).grid(row=r, column=0, columnspan=5, sticky="ew", pady=10)
    stick_values = [label for _, label in STICK_CHOICES]
    stick_code = {label: code for code, label in STICK_CHOICES}
    left_var, right_var = tk.StringVar(), tk.StringVar()
    mouse_var, scroll_var = tk.StringVar(), tk.StringVar()
    for j, (text, var) in enumerate((("左摇杆", left_var), ("右摇杆", right_var))):
        ttk.Label(frm, text=text).grid(row=r + 1 + j, column=0, sticky="w", pady=2)
        ttk.Combobox(frm, textvariable=var, values=stick_values, state="readonly",
                     width=18).grid(row=r + 1 + j, column=1, sticky="w")
    for j, (text, var, key) in enumerate((("鼠标速度", mouse_var, "mouse_speed"),
                                          ("滚动速度", scroll_var, "scroll_speed"))):
        lo, hi = SPEED_RANGES[key]
        ttk.Label(frm, text=text).grid(row=r + 1 + j, column=2, sticky="e", padx=(0, 6))
        ttk.Spinbox(frm, textvariable=var, from_=lo, to=hi, increment=100,
                    width=8).grid(row=r + 1 + j, column=3, columnspan=2, sticky="w")

    def fill(c):
        stop_record()
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

    # 录入按键（Windows 下 Tk 的 keycode 就是虚拟键码）
    tk_mods = {16: "shift", 17: "ctrl", 18: "alt", 91: "win", 92: "win"}

    def on_key(e):
        bid = recording["bid"]
        if not bid:
            return None
        if e.keycode in tk_mods:
            name = tk_mods[e.keycode]
            if name not in recording["mods"]:
                recording["mods"].append(name)
            return "break"
        name = VK_TO_NAME.get(e.keycode, f"vk{e.keycode:02x}")
        rows[bid][1].set("+".join(recording["mods"] + [name]))
        stop_record()
        return "break"

    def on_key_up(e):
        bid = recording["bid"]
        if not bid:
            return None
        if e.keycode in tk_mods and recording["mods"]:   # 只按了修饰键，例如单独的 shift
            rows[bid][1].set("+".join(recording["mods"]))
            stop_record()
        return "break"

    root.bind("<KeyPress>", on_key)
    root.bind("<KeyRelease>", on_key_up)

    def save():
        stop_record()
        new = {"version": 1, "buttons": {}, "repeat": []}
        for bid, (choice, keytext, rep, *_) in rows.items():
            code = action_code[choice.get()]
            if code == "key":
                text = keytext.get().strip().lower().replace(" ", "")
                try:
                    parse_combo(text)
                except ValueError as e:
                    messagebox.showerror(SETTINGS_TITLE, f"「{BUTTON_LABELS[bid]}」的键盘按键无效：{e}",
                                         parent=root)
                    return
                code = "key:" + text
                if rep.get():
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
                messagebox.showerror(SETTINGS_TITLE, f"{text}要在 {lo}～{hi} 之间。", parent=root)
                return
            new[key] = value
        try:
            save_config(new)
        except OSError as e:
            messagebox.showerror(SETTINGS_TITLE, f"保存失败：{e}", parent=root)
            return
        log("改键设置已保存")
        messagebox.showinfo(SETTINGS_TITLE, "已保存。PadMouse 正在运行的话，新按键会在 1 秒内生效。",
                            parent=root)
        root.destroy()

    btns = ttk.Frame(frm)
    btns.grid(row=r + 4, column=0, columnspan=5, sticky="e", pady=(14, 0))
    ttk.Button(btns, text="恢复默认", command=lambda: fill(DEFAULT_CONFIG)).pack(side="left", padx=4)
    ttk.Button(btns, text="取消", command=root.destroy).pack(side="left", padx=4)
    ttk.Button(btns, text="保存", command=save).pack(side="left", padx=4)

    fill(cfg)
    root.mainloop()


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
        else:
            run()
    except Exception:
        fatal("启动失败：\n\n" + traceback.format_exc())
