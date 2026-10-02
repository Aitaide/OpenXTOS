# XTOS SDK ABI 版本策略

**状态**: 生效（2026-10-02）
**适用**: `sdk/libxtos-rt`、`sdk/libxtos-signal`、`sdk/libxtos-bci`

---

## 版本号

三个库各自维护 `include/xtos/<lib>_version.h`：

```c
#define XTOS_RT_VERSION_MAJOR 1
#define XTOS_RT_VERSION_MINOR 0
#define XTOS_RT_VERSION_PATCH 0
#define XTOS_RT_VERSION_STRING "1.0.0"
```

## 语义

| 位 | 变更时机 |
|----|----------|
| MAJOR | 任何 ABI 破坏：符号删除/签名变更、导出结构体布局变更、ioctl 语义不兼容 |
| MINOR | 仅新增符号/新增结构体字段（向后兼容） |
| PATCH | 行为修复、性能变更，不影响 ABI |

基线 **1.0.0**（2026-10-02）。BCI 库的整型化 UAPI（阻抗 `uint32_t[32]` kOhm、量程 `uint32_t` mV；ioctl 编号不变）记为 major 1 的起点，头文件中有注释说明。

## 文件名约定

| 文件 | 来源 |
|------|------|
| `lib<name>.so.MAJOR.MINOR` | 构建产物（真实文件） |
| `lib<name>.so.MAJOR` | SONAME（动态链接器解析用） |
| `lib<name>.so` | 开发符号链接 |

Makefile 中 `LIB_MAJOR`/`LIB_MINOR` 必须与对应头文件保持一致，由 `scripts/check_abi.sh` 在 CI/测试中强制校验（比对 `objdump -p` 读出的 SONAME 与头文件声明）。

## 版本变更流程

1. 修改 `<lib>_version.h`（MAJOR/MINOR/PATCH 相应位 +1）
2. 同步修改对应 Makefile 的 `LIB_MAJOR`/`LIB_MINOR`
3. 若 MAJOR 变更：更新 `docs/` 中受影响文档与下游示例，提交信息注明 ABI break
4. `make clean all && ./scripts/check_abi.sh` 必须通过

## 检查接入

- 本地/CI：`./scripts/check_abi.sh`（已接入 `scripts/test.sh`）
- 检查内容：SONAME 与头文件一致、so 文件名与头文件一致

## 当前导出面（参考基线）

- libxtos-rt：实时工具（`rt.c`）+ SPSC 环形队列（`spsc.c`）
- libxtos-signal：时间戳/FFT/陷波/带通/SSVEP/P300 特征（`signal.c`）
- libxtos-bci：设备会话与 ioctl 封装（`session.c`）
