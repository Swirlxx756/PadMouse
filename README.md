# PadMouse 手柄鼠标

用 Xbox 手柄代替鼠标的 Windows 小工具。在托盘里安静运行，所有按键都可以改，也可以设成空键。

- 左摇杆移动鼠标，右摇杆滚动，A/B/X 是左/右/中键（按住 A 可拖拽）
- 按住扳机切换精准慢速 / 加速
- 托盘图标显示状态：绿色运行中，灰色已暂停，红色没有手柄
- 改键窗口：每个键可设为鼠标键、键盘按键或组合键、暂停、慢速/加速、屏幕键盘，或「无（空键）」
- 保存后立即生效，不用重启

支持 Xbox 手柄和切到 XInput 模式的第三方手柄。PS 手柄可先用 DS4Windows 或 Steam 转成 XInput。

## 下载

到 [Releases](../../releases) 下载 `PadMouse.zip`，解压后双击 `PadMouse.exe`。不需要安装 Python。

首次运行时 Windows 可能弹出 SmartScreen 提示，点「更多信息」→「仍要运行」。

启动后右下角会弹出「PadMouse 已启动」通知。Windows 11 默认把新托盘图标收在 **^** 里，可以把它拖到任务栏上常驻。

## 使用

| 操作 | 效果 |
| --- | --- |
| 左键单击托盘图标 | 暂停 / 恢复 |
| 右键托盘图标 →「改键设置…」 | 打开改键窗口 |
| 右键托盘图标 →「当前按键」 | 查看现在的按键设置 |
| 右键托盘图标 →「退出」 | 退出 |

### 默认按键

| 手柄 | 功能 |
| --- | --- |
| 左摇杆 | 移动鼠标 |
| 右摇杆 | 滚轮（上下 / 左右） |
| A / B / X | 鼠标左键（按住可拖拽）/ 右键 / 中键 |
| Y | 屏幕键盘 |
| LT 按住 / RT 按住 | 精准慢速 / 加速 |
| LB / RB | 浏览器后退 / 前进 |
| 十字键 | 方向键（按住连发） |
| Back（两个方框） | Esc |
| Start（三条横线） | 暂停 / 恢复 |
| 按下左摇杆 / 右摇杆 | 任务视图（Win+Tab）/ 开始菜单（Win） |

### 改键

在改键窗口里，每个键（包括 LT/RT）都可以选：

- **无（空键）**：按了什么都不做
- 鼠标左 / 右 / 中键，鼠标侧键（后退 / 前进）
- **键盘按键 / 组合键**：点「录入」后在键盘上按一下，或直接输入，如 `ctrl+c`、`alt+tab`、`f5`、`win+d`；可勾选「按住连发」
- 暂停 / 恢复、精准慢速、加速、屏幕键盘、退出 PadMouse

左右摇杆可以设为移动鼠标、滚轮或无，鼠标速度和滚动速度也能调。

设置保存在 `%LOCALAPPDATA%\PadMouse\config.json`，出问题时可查看 `%LOCALAPPDATA%\PadMouse\padmouse.log`。

## 从源码运行

需要 Windows 和 Python 3。

```bash
pip install -r requirements.txt
pythonw pad_mouse.py
```

其他命令：

```bash
pythonw pad_mouse.py --settings
```

```bash
python pad_mouse.py --check
```

`--settings` 直接打开改键窗口，`--check` 检测手柄是否连接。

## 打包

双击 `build.bat`，生成 `dist\PadMouse.exe`。
