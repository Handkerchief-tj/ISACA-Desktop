# SLiCAP 5.2.1 迁移记录

## 目标

本目录记录从旧版 SLiCAP 4.0.8 原型迁移到 5.2.1 的事实、决策、测试结果和已知
限制。任何版本差异都应先在适配层消化，再暴露给界面和 SFG 算法。

## 当前里程碑

| 里程碑 | 状态 | 结果 |
|---|---|---|
| M0 冻结旧基线 | 完成 | 旧环境保留，算法 `52 passed`，建立本地 tag |
| M1 隔离环境/仓库 | 完成 | Python 3.12、SLiCAP 5.2.1、独立单仓库 |
| M2 SLiCAP 适配层 | 完成首版 | 公共数值 API + 版本检查 + 独立任务目录 |
| M3 参数系统 | 完成首版 | 来源追踪、工程后缀、严格模式、版本化默认值 |
| M4 官方 schematic | 完成首版 | 桌面端直接复用官方 MainWindow、CanvasPanel 与数据格式 |
| M5 FastAPI | 完成首版 | 转换、任务队列、结果和制品接口 |
| M6 两条分析主线 | 已接通 | `demo_2_numeric` 数值与内置 SFG 算法端到端通过 |
| M7 桌面集成 | 完成开发版 | 官方画布、隔离 worker、结果页面和任务取消 |

## 记录文件

- `architecture.md`：分层边界与数据流。
- `parameter-policy.md`：参数解析、默认值和数值完整性规则。
- `compatibility-matrix.md`：4.0.8 与 5.2.1 兼容性结论。
- `baseline-2026-09-04.json`：机器可读的当前验证快照。
- `../desktop/teammate-setup.md`：新开发者安装、启动和验收步骤。
- `../desktop/monorepo-baseline-2026-09-07.md`：单仓库整理、打包和端到端验收记录。

## 下一步

1. 将受控源、MOS、BJT fixture 扩展为官方 GUI 保存后的往返 fixture。
2. 完成 standalone 构建、许可证审计和干净 Windows 验收。
3. 设计现有网站通过结构化 API 调用分析核心的独立集成方案。
