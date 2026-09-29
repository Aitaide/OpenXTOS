# XTOS - eXtended real-Time Operating System

全球首个面向AGI驱动的脑机接口应用优化的实时操作系统

## 项目状态

- **版本**: 开发中 (目标 v1.0.0)
- **开始日期**: 2026年
- **开发模式**: AI Agent全自动开发

## 快速链接

- [技术规范](docs/XTOS_Technical_Specification.md)
- [开发计划](plans/12_Month_Development_Plan.md)
- [API文档](docs/api/) (开发中)
- [示例代码](examples/) (开发中)

## 核心特性

- ✓ 超低延迟: 调度延迟<10μs, EEG处理<15ms
- ✓ 多架构支持: x86_64, ARM64, RISC-V
- ✓ 通用EEG设备: OpenBCI, Emotiv, Muse等
- ✓ 多AI框架: PyTorch, TensorFlow, ONNX Runtime
- ✓ 零样本学习: 5分钟快速校准
- ✓ AR/VR集成: 基于OpenXR标准

## 目录结构

```
xtos-project/
├── docs/              # 技术文档
├── plans/             # 开发计划
├── kernel/            # 内核源码 (开发中)
├── drivers/           # 设备驱动 (开发中)
├── sdk/               # 用户空间SDK (开发中)
├── tools/             # 开发工具 (开发中)
├── examples/          # 示例应用 (开发中)
├── tests/             # 测试套件 (开发中)
└── scripts/           # 自动化脚本 (开发中)
```

## 当前进度

**阶段1: 基础设施 (M1-M3)** - 进行中
- [ ] M1: 环境搭建与内核原型
- [ ] M2: 调度器核心实现
- [ ] M3: 驱动框架与设备支持

## 联系方式

- 项目主页: https://github.com/xtos-project/xtos (筹建中)
- 文档站点: https://xtos.ai (筹建中)

## 许可证

- 内核部分: GPL v2 (Linux内核要求)
- SDK部分: Apache 2.0
- 专有算法: 商业授权

---

**注**: 本项目由AI Agent自动化开发，完整技术方案和进度计划详见 docs/ 和 plans/ 目录。
