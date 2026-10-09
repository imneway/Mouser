# Mouser fork — Mac 端交接说明

> 给 Mac 上的 Claude 会话看。用户是设计师、不读代码：用中文、用大白话汇报"改了什么、好没好"；验证由你负责（构建、装上、看日志）。

## 背景

- 用户一套键鼠通过 KVM 在 Windows PC 和 Mac mini 之间切换。鼠标是 MX Master 3S，走 **Logi Bolt 接收器**（接收器插在 KVM 上，所以每次切换，接收器会在当前电脑上"拔掉/插上"一次）。
- 用户只用 Mouser 的 **DPI 设置**（PC 上 dpi=3600）。PC 上按键交给 XMBC 管；Mac 上按键怎么管，按用户现状来，不要主动改。
- 仓库：`imneway/Mouser`（fork 自 `TomBadash/Mouser`），分支 **`fix/late-slot-reply`**。这个分支上的提交信息用**英文**（以后可能给上游提 PR），一个修复一个提交，结尾带 Co-Authored-By。

## 这个分支已经修了什么（PC 上已实测）

全部改在共用代码 `core/hid_gesture.py` / `core/engine.py` 里，**Mac 上用这个分支重新构建就自动带上**，不用再改：

| 提交 | 解决的问题 | 用户感受 |
|---|---|---|
| cc9d8a8 | 上游 bug：探测槽位时只等 400 ms，且不比对应答来自哪个槽位，迟到的 1 号槽应答被记到空的 2/3 号槽，之后每条请求白等 2 s | 切回来后要等 35 s 以上才认到鼠标（用户说 Mac 上也"相当慢"，就是这个） |
| 81d2c3a | 先向接收器 6 个槽位同时广播一次探测，按应答顺序连接 | 识别更快更稳 |
| d2993c3 | 新增 `HidGestureListener.notify_device_change()`：系统报告"有设备插拔"时立刻重试，不再等退避（最长 30 s）；连上后写回 DPI 前的等待 3 s → 0.5 s | **PC 上**接收器出现后约 2 s 连上、再 1 s DPI 到位 |
| 1a06f4c | 优先探测接收器的长报文接口（usage 0x0002） | 缓存失配时省约 3 s |
| 3b7624e | 刚连上鼠标正好打盹导致 DPI 写失败 → 记下来，鼠标醒来时补写 | 偶尔 DPI 没生效的情况消失 |
| 2ff8ec1 | 鼠标关电源再开：收到电量广播或醒来时回读 DPI，和上次设置的不一致就补写 DPI + Smart Shift + 按键接管（5 s 限频） | 关开鼠标后不用手动拖 DPI 滑块（**PC 上已安装，尚未实测**） |

测试：`python -m unittest tests.test_hid_gesture` 在 Windows 上全过。上游在 Windows 上本来就有 15 个 Linux/macOS 专属测试失败——**在 Mac 上跑一遍完整测试，确认这些在 Mac 上是过的**。`tests.test_engine` 里 `test_battery_poll_skips_smart_shift_reads_while_replay_is_inflight` 在电脑空闲时会假失败（和改动无关）。

## Mac 上值得补的（需要写代码）

### 1. 接收器插拔通知 → `notify_device_change()`（最重要）

现状：Windows 靠 `core/mouse_hook_windows.py` 里的 `WM_DEVICECHANGE` 调 `hg.notify_device_change()`（见 `_on_device_change`）。**Mac 上没有对应的东西**——`core/mouse_hook_macos.py` 只监听了系统唤醒/屏幕唤醒/切换用户（`_register_wake_observer`），上游文档也写着 Mac 是"HID++ reconnect loop"。

后果：KVM 切到 PC 待了几分钟，Mac 上的重连退避已经涨到 30 s；切回 Mac 时最坏要等 30 s 才开始探测。这正是 PC 上 d2993c3 解决的问题，Mac 还没解决。

建议做法：
- 在 Mac 上监听"罗技设备（VID 0x046D）出现/消失"，回调里只做一件事：`hg.notify_device_change()`。
- 首选 `IONotificationPortCreate` + `IOServiceAddMatchingNotification`（匹配 `IOHIDDevice` + `VendorID=0x046D`，`kIOFirstMatchNotification` 和 `kIOTerminatedNotification`），挂到一个专用线程的 CFRunLoop 上。这种方式**不打开设备**，不会和 Mouser 自己的 HID 访问抢；回调里要把 iterator 里的对象逐个 `IOObjectRelease` 掉（不 drain 的话下次不会再通知）。
- 也可以用一个**常驻、全程只建一次**的 `IOHIDManager` + `IOHIDManagerRegisterDeviceMatchingCallback`。千万不要每次重连都新建 manager——上游 issue #238 就是 IOHIDManager 泄漏（见 `MEMORY_LEAK_PLAN.md`、`_close_manager` 注释）。
- 放的位置参照 Windows：由 mouse hook 持有、`start()` 里注册、`stop()` 里注销，回调里取 hid listener 的写法照抄 `mouse_hook_windows.py` 的 `_on_device_change`（`hasattr(hg, "notify_device_change")` 那段）。
- 加一个测试（参照 `tests/test_hid_gesture.py` 里中断退避那个测试，或给 macOS hook 写个 mock 测试），独立一个英文提交。

### 2. 唤醒后顺带校验一次状态（可选，小改）

Mac 的唤醒处理会 `force_reconnect()`，重连后 engine 会回放设置，一般够了。如果实测发现 Mac 睡醒后 DPI 偶尔不对，可以在 `_resume_recovery_worker` 里加调一次 `hg.request_state_verify()`（2ff8ec1 新增的公开方法）。**没出问题就别加。**

## Mac 上要检查的（不用写代码）

1. **别的罗技软件抢 HID++**：看 Mac 上有没有 Logi Options+ / Logi Bolt app / G HUB 在后台跑或开机自启（登录项、`~/Library/LaunchAgents`、`/Library/LaunchAgents`）。它们和 Mouser 同时跑会互相抢鼠标。PC 上 Logi Bolt app 的自启动已删。删任何自启动前先问用户。
2. **自动更新要关**：`~/Library/Application Support/Mouser/config.json` 里 `check_for_updates` 设为 `false`，否则上游新版会覆盖 fork 版本。改 JSON 时别破坏格式（PC 上曾因文件头多了 BOM 被 Mouser 当成坏文件重置，DPI 丢了）。
3. **权限**：自己构建的 app 签名会变，系统设置 → 隐私与安全性里的「辅助功能」和「输入监控」可能要把旧的 Mouser 条目删掉、重新添加新的 app，然后重启 Mouser。
4. **开机自启**：沿用 Mac 上原来的方式（登录项）即可，确认指向新装的 app。
5. **指针手感**：PC 上的"漂得远"是 XMBC 偷偷开了指针加速，已在 PC 上改回。Mac 的手感是用户认可的基准，**不要动 Mac 的指针速度/加速设置**。

## 构建与安装（Mac）

1. 拉代码：`git fetch origin && git checkout fix/late-slot-reply && git pull`（仓库没 clone 过就 `git clone https://github.com/imneway/Mouser.git`）。
2. 按 `readme_mac_osx.md` 准备 Python 3.11+ 的 venv（Apple Silicon 用 arm64 的 Python），`pip install -r requirements.txt`。
3. 跑测试，再 `./build_macos_app.sh` 构建 `.app`。
4. 先退出正在跑的 Mouser，把 `/Applications` 里的旧版**改名备份**（别删），放入新版，处理权限（上面第 3 条），启动。
5. 把 Mac 端的改动（如有）按英文提交、push 到同一分支；PC 那边之后会拉取重新构建。

## 验证（日志：`~/Library/Logs/Mouser/mouser.log`）

请用户逐项操作，你看日志确认：

| 场景 | 用户操作 | 日志里应看到 |
|---|---|---|
| KVM 切回 Mac | 在 PC 待 1 分钟以上再切回 Mac | 接收器出现后几秒内出现连接成功和 `DPI set to …`；补了第 1 项之后应在 ~3 s 内，而不是十几到 30 s |
| 鼠标关电源再开 | 关电源，等几秒，打开并动一下 | `mouse was power-cycled; restoring settings`，随后 `DPI set to …` |
| 睡眠唤醒 | Mac 睡眠后唤醒 | `Resume detected` → 重连 → `DPI set to …` |
| 鼠标打盹 | 放着不动几分钟再动 | `Device woke from sleep`，DPI 不变 |

PC 端相关资料：PC 安装在 `C:\Program Files\Utilities\Mouser`（计划任务提权启动，原因见上游 issue #270），与 Mac 无关，不用管。

## Mac 端进度（2026-10-09）

- 第 1 项已完成：提交 1f1ca3e 新增 `core/macos_device_monitor.py`，用 `IOServiceAddMatchingNotification` 监听罗技 HID 设备出现/消失，由 macOS mouse hook 在 `start()`/`stop()` 里启停，回调调 `notify_device_change()`。日志关键字：`[DeviceMonitor] Watching for Logitech HID arrivals/removals`、`[MouseHook] Logitech device connected — re-probing`。
- 第 2 项（唤醒后 `request_state_verify()`）没做，等实测出问题再加。
- Mac 上完整测试：除 5 个上游 master 本来就失败的测试外全过（`test_wheel_divert.MacOSSuppressionTests` 4 个：装了真 PyObjC 时测试替换不了 Quartz；`test_engine` 那个空闲时假失败）。
- 已装到 `/Applications/Mouser.app`，官方 3.7.3 备份为 `/Applications/Mouser-official-3.7.3.app`；`check_for_updates` 已设为 false；开机自启 LaunchAgent 指向新 app。
- Mac 上没有 Logi Options+ / Bolt app / G HUB；有 SteerMouse 和 Mos 常驻（用户原有配置，未动）。
- 构建用 uv 建的 Python 3.12 venv（与上游 CI 一致）：`uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt`。ad-hoc 签名每次重建都会变，重装后要重新授权「辅助功能」和「输入监控」。
- 提交 8f6c3ef 新增「在菜单栏显示图标」开关（设置页启动选项里，键 `show_menu_bar_icon`，仅 macOS）。关掉后靠 `rapp`（重新打开 app）事件回到设置窗口：聚焦搜索 / 启动台 / 访达再打开一次 Mouser 即可。图标隐藏时退出 Mouser 要先把开关打开，从菜单栏菜单退出（Cmd+Q 只会隐藏窗口）。
