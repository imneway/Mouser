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
| 2ff8ec1 | 鼠标关电源再开：收到电量广播或醒来时回读 DPI，和上次设置的不一致就补写 DPI + Smart Shift + 按键接管（5 s 限频） | 关开鼠标后不用手动拖 DPI 滑块（**PC 2026-10-10 实测通过**：开机即收到电量广播 → 回读 1000 → 同一秒内补回 3600 + Smart Shift + 按键接管） |
| PC 2026-10-10 | 新增 `HidGestureListener.notify_user_input()`：未连接时指针一动就打断重连退避（已连接时直接返回，2 s 限频）。接在 `BaseMouseHook.note_pointer_activity()` 开头，所以 **Mac 的事件 tap 已经自动带上，不用再接**；Windows 钩子也改为调 `note_pointer_activity()`（跳过注入事件），PC 顺带拿到 Mac 的「闲置 ≥30 s 后回读 DPI」 | PC 睡醒后鼠标沉睡、探测全超时、退避涨到 30 s，动了鼠标还要等——实测近 1 分钟才连上；修后一动鼠标约 1 s |
| PC 228a4c1 | Windows 处理 `WM_POWERBROADCAST` 睡醒事件（和 Mac 的 `NSWorkspaceDidWakeNotification` 对等）：未连接 → 立即探测；已连接 → 回读一次 DPI | 睡醒后不再傻等退避；已用假唤醒消息实测触发 |
| PC c87f1ec | 一轮探测里，接收器的长报文接口（usage 0x0002）打开了但 6 个槽都没应答时，跳过它的短报文接口（0x0001，本来就发不了请求） | 鼠标沉睡时一轮失败探测从 ~9 s 缩到 ~4.5 s，动鼠标后的等待上限也随之减半 |
| PC dfa1c66 | `_verify_device_state` 5 s 限频内的校验请求改为"延后"而不是"丢弃" | 睡醒那次校验若撞上鼠标还在打盹而超时，几秒后用户动鼠标触发的那次不再被吞掉 |

测试：`python -m unittest tests.test_hid_gesture` 在 Windows 上全过。上游在 Windows 上本来就有 15 个 Linux/macOS 专属测试失败——**在 Mac 上跑一遍完整测试，确认这些在 Mac 上是过的**。`tests.test_engine` 里 `test_battery_poll_skips_smart_shift_reads_while_replay_is_inflight` 在电脑空闲时会假失败（和改动无关）。

## Mac 上值得补的（需要写代码）

### 1. 接收器插拔通知 → `notify_device_change()`（已完成，见文末「Mac 端进度」，提交 1f1ca3e）

以下是当初的背景，保留备查。原状：Windows 靠 `core/mouse_hook_windows.py` 里的 `WM_DEVICECHANGE` 调 `hg.notify_device_change()`（见 `_on_device_change`）。**Mac 上没有对应的东西**——`core/mouse_hook_macos.py` 只监听了系统唤醒/屏幕唤醒/切换用户（`_register_wake_observer`），上游文档也写着 Mac 是"HID++ reconnect loop"。

后果：KVM 切到 PC 待了几分钟，Mac 上的重连退避已经涨到 30 s；切回 Mac 时最坏要等 30 s 才开始探测。这正是 PC 上 d2993c3 解决的问题，当时 Mac 还没解决。

当初的建议做法（已按此实现）：
- 在 Mac 上监听"罗技设备（VID 0x046D）出现/消失"，回调里只做一件事：`hg.notify_device_change()`。
- 首选 `IONotificationPortCreate` + `IOServiceAddMatchingNotification`（匹配 `IOHIDDevice` + `VendorID=0x046D`，`kIOFirstMatchNotification` 和 `kIOTerminatedNotification`），挂到一个专用线程的 CFRunLoop 上。这种方式**不打开设备**，不会和 Mouser 自己的 HID 访问抢；回调里要把 iterator 里的对象逐个 `IOObjectRelease` 掉（不 drain 的话下次不会再通知）。
- 也可以用一个**常驻、全程只建一次**的 `IOHIDManager` + `IOHIDManagerRegisterDeviceMatchingCallback`。千万不要每次重连都新建 manager——上游 issue #238 就是 IOHIDManager 泄漏（见 `MEMORY_LEAK_PLAN.md`、`_close_manager` 注释）。
- 放的位置参照 Windows：由 mouse hook 持有、`start()` 里注册、`stop()` 里注销，回调里取 hid listener 的写法照抄 `mouse_hook_windows.py` 的 `_on_device_change`（`hasattr(hg, "notify_device_change")` 那段）。
- 加一个测试（参照 `tests/test_hid_gesture.py` 里中断退避那个测试，或给 macOS hook 写个 mock 测试），独立一个英文提交。

### 2. 唤醒后顺带校验一次状态（Windows 已做：228a4c1；Mac 看情况）

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
- 构建用 uv 建的 Python 3.12 venv（与上游 CI 一致）：`uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt`。签名已固定：登录钥匙串里有自签名证书「Mouser Local Signing」（SHA-1 603211C0…5A3C，10 年有效），身份写在 gitignored 的 `.env.local`。构建用 `set -a; source .env.local; set +a; ./build_macos_app.sh`，重建后系统授权保持有效；不带 `.env.local` 会退回 ad-hoc 签名，授权又会失效。
- 提交 8f6c3ef 新增「在菜单栏显示图标」开关（设置页启动选项里，键 `show_menu_bar_icon`，仅 macOS）。关掉后靠 `rapp`（重新打开 app）事件回到设置窗口：聚焦搜索 / 启动台 / 访达再打开一次 Mouser 即可。图标隐藏时退出 Mouser 要先把开关打开，从菜单栏菜单退出（Cmd+Q 只会隐藏窗口）。
- 2026-10-10 实测发现：鼠标会在没有任何链路事件的情况下自己把 DPI 退回 1000（14:46:44 写入 3600 且设备回显确认，几分钟后直接回读是 1000，日志中间没有任何记录）。电量轮询不会触发校验（只有鼠标主动广播才会），而且轮询只在设置窗口打开时跑。提交 6481188 加了「指针闲置 ≥30 s 后再动就回读一次 DPI」（`BaseMouseHook.note_pointer_activity`，macOS 事件 tap 调用），不一致就走 `_verify_device_state` 补写 DPI / Smart Shift / 按键接管。日志关键字：`Device reports DPI 1000, expected 3600 -- mouse was power-cycled; restoring settings`。PC 端可在 `WH_MOUSE_LL` 的 `WM_MOUSEMOVE` 里照样调 `note_pointer_activity()`。
- 这条补写路径（2ff8ec1 + 6481188）在真机上还没触发过；冷启动时 Mouser 走 hidapi 独占打开鼠标，外部脚本无法同时读写 HID++ 做人工复现，唤醒后重连走 IOKit 非独占时才可以（`core/hid_gesture._MacNativeHidDevice(0xC548, 0xFF00, 0x0001)`）。
- Codex 对 6481188 的提醒（已知、未改）：事件 tap 分不清指针动作来自哪台设备。若用户同时用触控板，触控板活动会刷新闲置计时（鼠标 DPI 重置后切回鼠标可能不触发校验），也可能在罗技鼠标仍在休眠时发一次回读（2 s 超时，连续 3 次会进入"设备休眠"状态，收到任何 HID++ 报文即恢复）。本机是 Mac mini + KVM 键鼠，没有触控板，先不处理。
- 2026-10-11 真机证据：补写路径已两次自动触发并成功（22:33:35 切回时鼠标刚醒就回读到 1000 → 补写；00:40:53 电量广播触发回读到 1000 → 补写）。但 00:38:41 切回后那次 DPI 写入被设备回显确认，2 分钟后仍变回 1000，中间没有任何链路事件。提交 f60100a 在每次连上后 15 s 和 150 s 各安排一次回读（`_arm_post_connect_verifies` / `_pop_due_followup_verify`），不再只靠广播或闲置触发。已拉取 PC 端 fe1bb58…d6f126c 一并构建安装。

- 提交 1541654：校验补写一律用最新设定的 DPI（Codex 审查发现旧的已确认值可能盖掉新值）。
- 2026-10-11 提交 879b71b + f82e105 新增「精细拖动」（设置页指针速度卡片下方，仅 macOS，键 `precision_drag_enabled` / `precision_drag_modifier` / `precision_drag_dpi`，默认关 / Command / 400）：按住修饰键用左键拖动时立即把鼠标写成低 DPI，松开左键或修饰键立即写回设定 DPI。实现要点：HID++ 监听线程会在 `_rx()` 里阻塞最多 1 s，所以 `shift_dpi` / `unshift_dpi` 直接在事件 tap 线程用 `_tx`（有锁）发 setSensorDpi、不等回复，且用独立的 software-id `SHIFT_SW`，避免回复或错误被监听线程当成自己请求的答案；shift 期间 `_verify_device_state` 不动作，unshift 后 2 s 安排一次回读兜底。Mac 侧是第二个只监听的 CGEventTap（左键按下/抬起/拖动 + flagsChanged），不碰原来那个主 tap。按应用：「鼠标」页建的应用配置档各有一行覆盖（跟随全局 / 关闭 / 自定义修饰键+DPI），存在 profile 的 `precision_drag` 字典里，引擎在切换配置档时下发。日志关键字：`[HidGesture] Precision drag: DPI -> 400` / `DPI -> 3600 (restored)`。已知：触控板的左键拖动也会触发（事件 tap 分不清设备），本机无触控板不处理；Windows 端要自己在 WH_MOUSE_LL 里喂 `precision_button()` / `precision_modifier()`。用户机器上已把该功能打开（Command / 400）。
- 173ac6d / 下一提交：精细拖动的按应用列表在新建配置档后立即刷新（原来只听 settingsChanged）；某行 DPI 滑块按住时推迟刷新，松手提交后再重建列表。

### 给 PC 端：同步精细拖动要做的事

核心层（config / hid_gesture / mouse_hook_base / engine / backend / QML / 文案）已经跨平台，PC 拉代码后直接可用，只差 Windows 的输入接线：

1. `ui/backend.py` 的 `supportsPrecisionDrag` 目前只在 darwin 返回 True，Windows 接好后改成 darwin + win32（设置卡片靠它显示）。
2. `core/mouse_hook_windows.py` 的 `WH_MOUSE_LL` 回调里，`_should_intercept_events()` 之后：
   - `WM_LBUTTONDOWN` → `self.precision_button(True, <修饰键是否按住>)`
   - `WM_LBUTTONUP` → `self.precision_button(False, <同上>)`
   - `WM_MOUSEMOVE` 且左键按住 → `self.precision_modifier(<同上>)`
   - 修饰键状态用 `GetAsyncKeyState`：command→VK_CONTROL（Windows 上建议把 "command" 解释为 Ctrl，或在 UI 里另给 Win 键选项），option→VK_MENU，control→VK_CONTROL，shift→VK_SHIFT；function 在 Windows 上读不到，忽略。
   - 只松修饰键不动鼠标的情况：可在已有键盘钩子（如有）里调 `precision_modifier()`，没有也行，下一次移动/松键就会恢复。
3. Windows hook 的 `stop()` 里，在 `_stop_hid_listener()` 之前调 `self._end_precision_drag()`，避免退出时停在低 DPI；钩子被系统超时摘掉重装时也调一次。
4. 只读回调不要拦截事件（精细拖动只写 DPI，不改指针事件）。
5. 测试参考 `tests/test_precision_drag.py` 里 Mac tap 那组，换成 Windows 消息。

### PC 端回复（2026-10-11）：Windows 精细拖动已接好

- 实现：`core/mouse_hook_windows.py` 在 `WH_MOUSE_LL` 里对 `WM_LBUTTONDOWN` / `WM_LBUTTONUP` 取样修饰键（`GetAsyncKeyState`）并调 `precision_button()`；左键按住期间的 `WM_MOUSEMOVE` 只在修饰键状态变化时调 `precision_modifier()`。取样在钩子线程，**实际的 `shift_dpi` / `unshift_dpi` 写入放到独立的 `PrecisionDrag` 工作线程**（队列串行）——低级钩子回调若被 hidapi 的 write 卡住（最长 1 s）会让全系统鼠标卡顿，Windows 还会把慢钩子摘掉。取样放在 KVM 守卫（`_should_intercept_events`）之前，和 Mac tap 一样；基类状态机决定写不写。
- 修饰键命名：Windows 设置页只列 Ctrl / Alt / Shift / Win（配置值 `control` / `alt` / `shift` / `win`，文案键 `scroll.precision_drag_winmod_*`）。`config.py` 新增 `PRECISION_DRAG_MAC_MODIFIERS` / `PRECISION_DRAG_WINDOWS_MODIFIERS`，总表 `PRECISION_DRAG_MODIFIERS` 两边都收。Windows 钩子把 Mac 名字映射到最近的键（command→Ctrl、option→Alt、function 永远不算按住）；Mac 的 `_PRECISION_FLAG_MASKS` 也加了 `alt`→Option、`win`→Command，配置跨机器也不坏。**默认修饰键改成按平台**：macOS 仍是 `command`，Windows 是 `control`（Figma 等把 ⌘ 快捷键对应 Ctrl）；`PRECISION_DRAG_DEFAULT_MODIFIER` 跟着默认配置走，`tests/test_precision_drag.py` / `tests/test_backend.py` 里三处断言改成读这个常量。
- `stop()`：先停工作线程（把队列里剩下的松键处理完）→ `_end_precision_drag()` → `_stop_hid_listener()`；`_reinstall_hook()`（插拔重装钩子）投递一个 `("end",)` 兜底松开。
- 测试：`tests/test_windows_hook_source.py` 新增 `PrecisionDragWindowsTests`（9 个，仅 Windows 跑）：Alt 拖动/松开、拖动中按下/松开修饰键、Mac 名字映射、注入事件忽略、钩子线程本身不写 HID、重装钩子兜底、stop 顺序、工作线程按序处理。
- 注意：用户的 PC 键盘是 Magic Keyboard 经 KVM，Windows 下 ⌘ 实际发 Win 键、⌥ 是 Alt、⌃ 是 Ctrl；默认选 Ctrl，用户想按 ⌘ 的话在设置里选 Win 即可。

### Mac 端进度（续）

- 2026-10-12 切回提速（fe6019d / 148e9bd / 2c78009 / febc62d）：① Mac 事件 tap 原来在"未连接就放行"判断之后才调 `note_pointer_activity()`，所以断开时晃鼠标叫不醒重连（PC 端顺序本来就对），已挪到判断前；② 重连成功后监听线程自己先排队写"最新想要的 DPI"，第一轮循环就写，不再等引擎回放；③ 引擎回放改成先写 DPI 再写 Smart Shift；④ Codex 发现 ② 引入的竞态：DPI 读写共用一个完成信号，旧写入完成时会把新请求当成已完成清掉。加了请求编号 `_dpi_req_gen`，只有编号对得上才算完成；断开期间设置的 DPI 记为最新想要值。日志关键字：`Re-applying DPI … on reconnect`。②③④ 是跨平台的，PC 拉代码即可。

### PC 端回复（2026-10-12）：切回再提速

- 已拉取 fe6019d / 148e9bd / 2c78009 / febc62d 一并构建。PC 日志里 10:24 那次切回：接收器 10:24:11 出现 → 第一轮 6 槽全超时（4.4 s）→ 长接口进入 10 s 冷却，于是第二轮开了接收器的短报文接口又白等 4 s（c87f1ec 只跳同一轮）→ 第三轮才连上 → 10:24:21 DPI 到位。
- 新提交（见 git log `fix(hid): spend less of a reconnect pass on slots that cannot answer`）：① 接收器同时列出长/短两个接口时，短接口一律跳过，并且不参与冷却计算（否则长接口冷却期间会没有候选可探）；② 广播无应答且有缓存槽位时，只补探缓存槽位一次（1.2 s），其余 5 个空槽不再逐个等 400 ms，下一轮照常广播全部槽位。一轮静默探测从 ~4.4 s 降到 ~2.4 s。跨平台，Mac 拉代码即可。
- 剩下的时间主要是鼠标自身重新链接接收器的耗时（接收器被 KVM 断电重枚举后，鼠标要等用户移动才会重连），软件侧控制不了。

### Mac 端进度（2026-10-13）

- 2026-10-13 息屏唤醒 DPI 丢失（e0712b3）：息屏期间鼠标自己深度休眠并完全重置（探测时 divert 状态全是 none），重连时写的 DPI 被确认但几秒内又丢，原先第一次回读在 15 s，用户有约 16 s 停在 1000。改成连上后 4 / 15 / 150 s 三次回读，且连上 2 s 后第一次移动指针也回读一次（`notify_user_input` 已连接分支，`_verify_on_input_after`）；回读撞上排队中的 DPI 写入时改成推迟到下一轮而不是丢掉；就地唤醒（`Device woke from sleep`）分支也同样布防。跨平台，PC 拉代码即可。
