MathAgent 机会式加速节点
========================

这个压缩包只包含平台加速节点，不是完整 MathAgent 项目。

首次安装
--------
1. 收到管理员发来的专属包时：解压后双击 `easy-setup.cmd`。不需要打开 PowerShell、输入
   命令、服务地址、Node ID 或 Token。
2. 收到通用包时：双击 `easy-setup.cmd` 后，按提示输入管理员提供的 HTTPS 服务地址、Node ID、
   Node Token。Token 输入时不会显示；凭据不会进入命令行参数或日志。
3. 专属包会在安装时将凭据写入本机 Windows Credential Manager，并删除包内的引导配置。
4. 安装完成后，只需双击 `control-platform-node.cmd` 开启、关闭或查看状态。

管理员制作专属邀请包
----------------------
管理员为此电脑创建平台节点后，可在项目目录执行（执行时会隐藏输入 Node Token）：

   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\desktop\scripts\make-platform-node-bundle.ps1 -ApiOrigin "https://你的服务地址" -NodeId "本机专用NodeId" -IncludeNodeToken -OutputPath ".\releases\MathAgent-PlatformNode-朋友电脑.zip"

带 `-IncludeNodeToken` 的专属包包含这台电脑的 HTTPS 地址、Node ID 和 Node Token。
安装完成后会自动删除 `node-bootstrap.json`。这类包只能
发给指定电脑的使用者，不要转发；通用包仍不包含任何凭据。

日常控制
--------
- 开启：控制菜单选择“开启节点”。
- 关闭：控制菜单选择“关闭节点”。关闭会停止计划任务并清理 sidecar 子进程。
- 查看：选择“查看状态”，确认 connected 和硬件状态。
- 关闭后状态中的 TaskEnabled 应为 False；再次使用“开启节点”即可恢复。
- 卸载：在 PowerShell 执行 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\uninstall-platform-node.ps1`，默认清除本节点的全部凭据。

要求
----
- Windows 10/11 x64。
- 允许出站 HTTPS；不需要公网 IP、端口映射或 SSH。
- NVIDIA 驱动正常时贡献 GPU 图片处理；CPU 也会作为受限回退。
- 模型 API 始终由服务器调用，节点不会接收或保存模型 API Key。
- 关闭节点只停止接收新任务；正在执行的任务会尽量收尾，超时会由服务器按失败重试规则处理。

安全说明
--------
- 专属包例外：它已包含指定节点的 Node Token，只能发送给该电脑的使用者，
  不得转发、上传网盘或发到群聊。安装完成后包内的引导配置会被删除。
- 通用包不包含 Node Token；不要把节点凭据写进脚本、群聊记录或截图。
- 每台电脑使用独立 Node ID/Token；节点关闭后服务器会停止分配新任务。
- 如果不再贡献，使用卸载脚本；是否保留本机凭据由卸载选项决定。

完整性校验
----------
- `manifest.json` 和 `SHA256SUMS.txt` 校验包内 sidecar。
- 压缩包旁边的 `.sha256` 文件校验整个 ZIP；发布者不会在包内放模型 API Key、数据库或学生文件。
- 通用包不含凭据；专属包只嵌入节点凭据，不会包含数据库、学生文件、
  `.env` 或服务器私钥。
