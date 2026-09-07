# ISACA Desktop

ISACA Desktop 是面向模拟电路教学与研究的本地桌面分析器。程序直接复用
SLiCAP 5.2.1 官方 Structured Electronic Design Environment 原理图画布，并在同一
应用中提供数值分析和按频率子区间执行的信号流图符号化简。

```text
官方 .slicap_sch / 手写 .cir
               |
               v
       规范化 SLiCAP 网表
          /             \
         v               v
 SLiCAP 数值分析       SFG 符号化简
 传函/极零点/MNA       根聚类/G_j/局部符号根
          \             /
           v           v
             桌面结果页
```

本仓库是完整单仓库版本：桌面界面、SLiCAP 适配层和 `sfg_prototype` 算法源码
都位于同一个 `src/`，不需要再克隆或安装第二个算法仓库。

## 当前状态

- 可从源码运行 PySide6 桌面开发版，绘图体验来自 SLiCAP 5.2.1 官方画布。
- 支持 `.slicap_sch` 绘制、保存、官方导出 `.cir` 和手写网表输入。
- 支持 Laplace 传递函数、DC 增益、极零点、MNA、Bode 和噪声等数值结果。
- 支持 SFG 构建、根聚类、频率子区间、误差受控图操作、局部符号根和报告。
- 视觉模块接口暂时保留，但根据当前项目决定不纳入本阶段测试与验收。
- 当前还不是免 Python 的独立 EXE；standalone 安装包属于下一交付阶段。

## 首次安装

只需要克隆这一个仓库。在 PowerShell 中执行：

```powershell
git clone https://github.com/Handkerchief-tj/ISACA-Desktop.git
cd ISACA-Desktop
conda create -n isaca_desktop python=3.12 -y
conda activate isaca_desktop
.\setup.ps1
```

`setup.ps1` 会一次性安装桌面端、SLiCAP 5.2.1 和仓库内置 SFG 算法，并运行
完整测试。已经配置好环境时可用 `.\setup.ps1 -SkipTests` 跳过回归。

## 启动

```powershell
conda activate isaca_desktop
cd <ISACA-Desktop 路径>
.\start-desktop.ps1
```

也可以直接运行：

```powershell
isaca-desktop
```

程序打开的是桌面窗口，不会给出浏览器网址。

## 基本流程

1. 在左侧项目面板新建或打开一个 SLiCAP 项目目录。
2. 使用中央官方画布新建或打开 `.slicap_sch`，设置器件参数、source 和 detector。
3. 保存原理图并导出网表，或在网表编辑器中打开现有 `.cir`。
4. 在右侧分析设置中选择数值分析、SFG 符号分析和频率范围。
5. 启动分析，在结果标签查看传函、极零点、MNA、根聚类和各频段符号表达式。

详细操作见 [桌面使用说明](docs/desktop/usage.md)。

## 目录结构

```text
src/isaca_desktop/   PySide6 界面、官方画布壳层、worker 与结果展示
src/isaca_api/       统一数据模型、参数系统和 SLiCAP 5.2.1 适配层
src/sfg_prototype/   完整 SFG 构图、化简、根定位和符号恢复算法
tests/desktop/       桌面、网表、参数和 SLiCAP 集成测试
tests/algorithm/     SFG 算法与论文样例测试
examples/            RC 与 demo_2 等可运行示例
docs/                使用说明、迁移记录和算法说明
scripts/             环境检查及可复现实验脚本
```

## 验证

```powershell
.\scripts\check-environment.ps1
```

该命令检查 Python 3.12、SLiCAP 5.2.1 和关键依赖，并统一运行桌面与算法测试。
不应提交 `runs/`、缓存、模型权重或 SLiCAP 自动生成的项目输出。

## 算法边界

当前算法已贯通论文 Fig. 12 的主要数据流，并能对论文单管示例恢复包括 Eq. (24)
在内的局部物理可读表达式。它仍属于科研原型：对于高阶、强耦合或拓扑异常的未知
电路，局部闭环根可能得到较长表达式或明确的 `UNRESOLVED`，不能将当前能力描述为
已经对任意复杂电路稳定产生最简解析式。

更完整的算法说明见 [SFG 算法说明](docs/algorithm/README.md)。

## 来源记录

本单仓库基线由以下已验证版本整理而来：

- 桌面集成基线：`Analog-Circuit-Analyzer` 提交 `425732e`。
- SFG 算法基线：`Intelligent-Symbolic-Analog-Circuit-Analyzer` 提交 `b7d5759`，
  原算法版本 `0.2.3`。

旧仓库继续保留历史，不是运行本项目的前置依赖。
