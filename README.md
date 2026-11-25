# eye-care-tray

Windows 护眼小助手：防蓝光色温调节 + 系统托盘常驻。
界面为原生 Vue3（无组件库依赖），桌面壳使用 pywebview（系统 WebView2）。

## 运行

- 双击桌面「护眼小助手」快捷方式，或直接运行 `dist/护眼小助手/护眼小助手.exe`。使用 Windows 原生标题栏，拖拽、最小化、关闭按钮都由系统提供；点「✕」收进托盘，退出请在托盘菜单中操作。
- 采用 folder（onedir）打包：启动无需解压，比单文件版明显更快。程序本体在 `dist/护眼小助手/`（含 `_internal` 依赖目录），移动或更新时需保持整个文件夹完整。
- 源码运行：`npm install && npm run build`，然后 `python main.py`（需 pywebview、pystray、Pillow）。
- 重新打包：`npm run dist`，产物在 `dist/护眼小助手/`。更新时先停掉正在运行的程序再覆盖该文件夹。

## 功能

- 护眼：拖动渐变轨道滑块调节强度，0% 为正常色温（6500K），100% 最暖（3400K）；写入 Windows Gamma Ramp，退出自动还原屏幕色彩。
- 开机自启动：页面开关读写当前用户注册表 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`，指向文件夹版 exe。
- 托盘菜单：显示/隐藏主窗口、退出；双击托盘图标可显示/隐藏窗口。
