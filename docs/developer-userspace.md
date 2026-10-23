# XTOS Developer 用户空间构建

状态：T04 用户空间层已完成，组装、运行、隔离修复与质量复审通过。本目录构建用户空间层，不能把它等同于正式可启动 USB Developer 镜像。

## 软件来源与构建范围

XTOS 内核仍由项目提供。日常开发用户空间使用 Debian trixie/main 与 trixie-security 的二进制包，锁定架构、版本、SHA256、字节大小和来源。获取包只下载，使用签名 InRelease/Packages 校验，不安装到控制端，不执行包的维护脚本。

当前正式维护的锁文件是 `system/packages/locks/x86_64.json`，含 213 个包、205770256 字节归档。Core/Developer 清单位于 `system/profiles/`；这一轮只完成 x86_64 包获取与运行检查，不能宣称 ARM64/RISC-V 已具有相同开发环境。

这套二进制版本是一个已实际获取的锁定基线，不代表最新上游版本；测试候选发布前仍需完成安全维护与工具链版本审查。当前实例包含 Python 3.13.5、Go 1.24.4、GCC 14.2、GNU nano 8.4、GNU wget 1.25.0。

## 获取新的版本锁

获取必须使用全新、空的目录，避免混入旧缓存包。脚本通过清理过的环境变量，将隔离配置放进 APT_CONFIG，让 APT 在读取宿主配置之前取得独立配置；不能只使用后读取的 `apt-get -c`。输出新锁先复核，再替换版本控制中的正式锁。

```sh
python3 scripts/fetch_userspace.py --profile developer --arch x86_64 --describe
python3 scripts/fetch_userspace.py \
  --profile developer --arch x86_64 \
  --work-dir "$PWD/.build/userspace/download-new" \
  --lock-output "$PWD/.build/userspace/lock-new.json"
```

依赖：控制端 Python 3、APT、Debian archive keyring、dpkg-deb；需要实际 HTTPS 网络。获取和离线组装是两步，已有已锁定归档无需重新获取。

## 离线组装与配置

输出必须是新的 `.build/userspace/` 子目录或 scratch 子目录。已有输出不会被清理或覆盖；失败只清理本次创建的 staging 目录。

```sh
bash scripts/build_userspace.sh \
  --lock system/packages/locks/x86_64.json \
  --packages-dir .build/userspace/locked-developer-download/packages \
  --output-dir .build/userspace/developer-new
python3 scripts/configure_userspace.py \
  --rootfs .build/userspace/developer-new
```

组装前检查包哈希、大小、实际 control 元数据、归档路径与架构；保留包版权文件。只解压 data payload，不运行 Debian postinst/prerm。配置步骤显式建立 sh/vi/awk 别名、CA bundle、锁定 root 账号、C.UTF-8 和本地 Go 工具链默认值；Go 遥测在 root 与新用户模板中明确设为 off，避免离线侧进程启动。它不会复制宿主机密码、私钥、DNS 服务器，也不启动 SSH 或网络服务。

`/usr/share/xtos/userspace-manifest.json` 是不可变的包来源与原始文件清单，显式配置结果另写 `userspace-provision.json`。配置后生成的新文件或别名不冒充原包内容。所有权检查逐级以 O_NOFOLLOW 打开 manifest，拒绝外部父链接；该检查是结构和路径契约，不是恶意二进制沙箱或密码学认证。

## ELF 验证与 Go 测试数据

```sh
python3 scripts/verify_userspace.py \
  --rootfs .build/userspace/developer-new \
  --lock system/packages/locks/x86_64.json \
  --elf-data-policy system/packages/elf-data-x86_64.json \
  --report .build/userspace/developer-new-elf.json
```

最终新组装 rootfs 已通过该 policy 下的检查：964 个 ELF 中 940 个为运行文件，24 个为精确登记的非运行数据，错误为 0。代码质量审查发现的问题已修复，最终复审通过。默认不提供 policy 时所有 ELF 都按目标架构处理，当前原始包层因此报告 41 个错误，分布在 Go 源码的 24 个测试样本和其他架构目标对象上。原始失败报告仍保留。

Go 源码保留用于本机编译，跨平台测试样本与运行程序必须区分。policy 按精确路径、包所有者和 SHA256 列明这些非运行数据；禁止用整个源码目录的通配忽略。运行目录中的文件或多层目录别名不得借 policy 绕过架构或依赖检查；运行文件的 PT_INTERP 解释器和 DT_NEEDED 动态库也不得解析到声明的非运行数据。新增两类真实 ELF 回归测试均先观察到错误接受，再确认修复后明确拒绝。包版本改变时重新核对路径和哈希。

ELF 静态验证检查目标架构、解释器和 DT_NEEDED 的目标 rootfs 路径闭包，不使用宿主 ldd。它不证明完整符号版本兼容，也不能替代真正启动和运行。

## 真实运行验收

```sh
python3 scripts/verify_userspace_runtime.py \
  --rootfs .build/userspace/developer-new \
  --report .build/userspace/developer-new-runtime.json
```

该命令需要控制端 root，仅对已验证来源的受信任用户空间执行。它通过 chroot 跑真实程序，不挂载宿主 `/dev`，只临时创建本目录的 `/dev/null`，结束时通过固定目录描述符和节点身份清理。目标进程执行前关闭 root 目录描述符，超时停止进程组。chroot 不是恶意 root 程序的安全边界。

验收覆盖程序版本、Python SSL/SQLite/ctypes/lzma 等标准库、venv/pip check、归档往返、vi 保存、loopback HTTPS可信证书接受/未知CA拒绝/主机名错误拒绝、C/C++、纯Go与CGO编译运行。nano 中文输入保存另做真实 PTY 验收。公共 DNS、在线 pip/Go 包获取、账号登录、时间同步和网络服务仍需后续客体验证。

## 已留存的证据

- `.build/userspace/locked-developer-download/evidence/lock-verified.json`：第一次包锁核对。
- `.build/userspace/independent-download-result.json`：修复 APT 隔离后的独立获取与锁文件一致性。
- `.build/userspace/reproducibility-result.json`：两次离线组装清单一致，35,185 个包路径当前字节、权限和链接一致，显式配置一致；不包含 ISO 字节/文件时间或运行时新生缓存的比较。
- `.build/userspace/developer-x86_64-final-elf.json`：最终新组装层的 940 个运行 ELF 与 24 个声明数据检查通过。
- `.build/userspace/developer-x86_64-final-runtime.json`：最终新组装层 12 项真实运行检查退出码 0、stderr 空。
- `.build/userspace/developer-x86_64-raw-elf.json`：默认严格检查的原始失败结果。
- `.build/userspace/developer-x86_64-runtime-fixed.json`：修复所有权/清理之后重新运行，12 项检查退出码 0 且 stderr 为空。
- `.build/userspace/nano-pty-acceptance.json`：nano 中文输入、保存、退出实测。
- `.build/userspace/qemu-developer-probe/result.json`、`serial.log`：QEMU TCG 使用既有 XTOS 6.6.0-rt15 内核，14 条命令均为 0，最终 `XTOS_DEVELOPER_GUEST=PASS`。

QEMU probe 证明已打包的用户空间能够在该 XTOS 内核上运行，但使用的是隔离的探针 initramfs；未实现正式 Live 镜像、未改动 H3C 服务器、未覆盖现有 U 盘、未进行内盘安装。无物理 Developer 镜像延迟、驱动或网络测试证据。

修复 APT 配置隔离后已在独立目录成功重新获取第二份包集合：锁文件与正式锁逐字段一致，213 个包共 205770256 字节全部重新通过 SHA256 和字节大小核对。T04 非运行 ELF policy、隔离修复复审及质量修复复审均已通过；40 项用户空间测试、10 项身份测试通过。源代码 SBOM 已重新生成并通过 freshness 检查；新增包来源仍由独立 lock 与 rootfs inventory 记录，完整 Developer artifact SBOM 属于后续 Live 封装工作。
