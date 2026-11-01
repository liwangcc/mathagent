# 文件导入测试夹具

本目录全部是本地生成的非学生素材，仅用于格式/解码回归，不是真实试卷或模型结果。`班级/张三/李四` 是测试目录身份。

- `page.jpg/png/gif/bmp/webp`：Pillow 生成同一张简单几何图，并以对应格式编码。
- `pages.zip`：Python `zipfile` 打包 JPEG/PNG，路径 `class/student-a/page.jpg`、`class/student-b/page.png`；用于检验嵌套目录与逐字节保真。
- `pages-rar4.rar`：按 RAR4 头结构生成的 STORE 单卷，头 CRC 与文件 CRC 使用 zlib.crc32；成员和 `pages.zip` 一致。它验证 RAR4 读取及 CRC，不代表穷举所有 RAR4 压缩参数。
- `pages-rar5.rar`：RAR 官方 Linux CLI 以 RAR5/压缩等级3生成的单卷，含 `page.jpg`、`page.png`。
- `unicode.rar`：同一 CLI 递归打包两个中文目录和重复 basename；必须输出两份独立路径。
- `encrypted.rar`、`encrypted-header.rar`：同一 CLI 生成的数据加密/头部加密负例；`encrypted.zip`：pyzipper 生成的 AES ZIP 负例。测试只验证拒绝，不需要也不记录解密口令。
- `pages.7z`：py7zr 打包 JPEG/PNG；`pages.tar` 及 `.gz/.bz2/.xz`：Python tarfile 生成。当前实现明确拒绝这些格式；它们不是支持能力样本。
- `duplicate.zip`：zipfile 写入相同路径两次，防止覆盖后伪装为成功。
- `unsupported.zip`：一个合法 JPEG 加一个 notes.txt，验证全选失败而非静默跳过。
- `symlink.zip`：Unix symlink 元数据，验证不接受特殊文件。
- `missing-footer.zip`：从普通 ZIP 移除目录/尾部，验证损坏包不会被当成部分成功。

复跑（frontend 目录）：

    node --test tests/archive-import.test.js tests/collection-import.test.js tests/browser-image-offload.test.js

额外负例由测试内构造：真实归档截断、ZIP/RAR CRC 修改、路径穿越、伪扩展名、数量/字节/比率上限、不可读文件、Worker 缺失及超时。`tests/helpers/archiveDecoder.js` 使用与浏览器相同的 JSZip/UnRAR 核心，不用假解码输出代替归档读取。

浏览器复跑：启动 Vite，打开 `/tests/import-browser.html`。该页调用 Upload.vue 中实际文件导入处理函数和真实 Worker/Canvas，仅在测试里替换服务可用性判断及消息收集；没有登录、不调用模型，不代表完整教师业务 E2E。
