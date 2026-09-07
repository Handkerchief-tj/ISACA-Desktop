# 本地系统架构

## 数据流

所有输入先转换为 `CircuitDocument`。它保存规范化网表、参数来源、诊断和输入来源，
因此数值分析与符号分析不需要知道电路最初来自图片、文本还是 schematic。

```text
Input adapters
  -> official-compatible .slicap_sch / raw netlist
  -> CircuitDocument
  -> SLiCAP521Adapter
       -> public SLiCAP numeric analyses
       -> bundled sfg_prototype engine
  -> AnalysisJob + artifacts
  -> QProcess worker -> PySide6 desktop results
  -> FastAPI (future website integration boundary)
```

## 并发边界

SLiCAP 当前具有解析器和项目配置的全局状态。`SLiCAP521Adapter` 使用进程内
`RLock` 串行化 SLiCAP 调用，并为每个任务创建独立工作目录。这个设计保证本地正确性；
未来服务器多进程部署应把每个分析放入独立 worker 进程，而不是移除锁。

## 算法边界

`sfg_prototype` 与桌面程序位于同一仓库、同一个 `src/` 源码树和同一个产品版本中。
桌面适配层仍只调用其公开入口和报告函数，算法不会直接依赖 Qt 界面。这样既保持
模块边界，又让开发者只需克隆、安装和测试一个仓库；版本回滚由整个产品提交完成。

## Schematic 边界

官方 `.slicap_sch` 是 schematic 的规范持久化格式。桌面程序直接继承 SLiCAP
5.2.1 `MainWindow` 并复用 `CanvasPanel`、符号库、引脚、导线和参数编辑功能，
不再维护自定义浏览器画布。保存后的 schematic 在隔离 worker 中调用官方加载与
网表导出逻辑生成 `.cir`；该 `.cir` 才是数值和 SFG 分析的权威输入。
