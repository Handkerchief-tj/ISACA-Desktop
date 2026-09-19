# ISACA Desktop

**ISACA（Intelligent Symbolic Analog Circuit Analyzer）** 是面向模拟电路教学与研究的
Windows 桌面分析工具，支持用户绘制电路原理图并生成电路网表，指定待分析端口和电路参数，输出传递函数、零极点等结果的数值解和基于信号流图进行化简的解析解。


## 技术路线与基本架构

ISACA 采用本地桌面分层架构。由 PySide6/Qt 构建原生 Windows
界面；原理图、网表、计算任务和结果文件均保存在用户本机。

### 桌面 UI 与交互层

主界面使用 **PySide6** 构建。左侧管理项目、原理图和网表，中央显示官方原理图画布或网表编辑器，右侧设置 source、detector、参数、分析类型和误差范围，底部显示任务进度与日志。

分析结果使用 **QWebEngineView** 承载本地 HTML 页面，通过随程序分发的 **KaTeX**
渲染公式；波特图等数值图形由 **Matplotlib** 生成，信号流图布局由 **Graphviz**
完成。

### SLiCAP 电路核心与适配层

原理图部分直接复用 [**SLiCAP 5.2.1**](https://www.slicap.org/) 官方编辑器及符号库。

保存原理图后，程序调用 SLiCAP 官方加载和导出逻辑生成 `.cir` 网表；手写 `.cir`
也可以直接进入同一流程。`isaca_api` 适配层固定检查 SLiCAP 版本，并统一处理网表
解析、层次展平、参数求值、连接与模型诊断。经过检查的网表随后用于 SLiCAP 的
拉普拉斯传递函数、极零点、MNA 矩阵、噪声和频率响应分析。

### SFG 分频段符号化简

`sfg_prototype` 负责将 SLiCAP 展平后的小信号网络转换为信号流图（SFG）。算法先
比较未化简 SFG 与 SLiCAP 参考传递函数，确认两者等价；再根据数值极点和零点在
对数频率轴上进行根聚类，将完整频率范围划分为若干子区间。

在每个子区间中，算法依据支路和回路的局部主导关系执行论文定义的图化简操作，并用
该频段内的传递函数幅值/相位误差决定是否接受操作。最终输出每个频段的化简 SFG、
可读符号传递函数、局部零极点表达式、数值频率、主导参数和误差记录，给出更适合分析电路工作机理的分频段解析结果。

### Worker、数据与发布层

原理图导出和电路分析通过 **QProcess** 启动独立 worker，桌面进程使用 JSON 请求和
结构化进度事件与其通信。SLiCAP 的项目级全局状态和耗时符号计算不会阻塞 UI；用户
可以取消任务，单个 worker 失败也不会破坏已经保存的项目。每次运行的请求、日志、
结果 JSON 和图像保存在项目的 `runs/` 目录，便于复查和复现。

发布版使用 **Nuitka** 生成 Windows standalone 目录，再由 **Inno Setup** 制作安装
程序。

## 核心功能

- **SLiCAP官方原理图编辑器**：使用 SLiCAP 的器件、引脚、连线、参数、和 `.slicap_sch` 保存格式。
- **双输入方式**：支持从官方原理图导出网表，也支持打开、编辑和验证 `.cir` 网表。
- **常规电路分析**：展示拉普拉斯传递函数、低频小信号增益、极点与零点、MNA
  矩阵、Bode 图和噪声结果。
- **SFG 符号化简**：提供根聚类、频率子区间、误差受控图操作、局部符号根和逐频段传递函数。
- **本地结果管理**：每次任务保存请求、日志、结构化结果和生成文件，便于复现与检查。

## 下载与安装

根据使用目的选择一种方式即可：

| 方式 | 需要自行安装环境 | 是否下载完整安装包 |
| --- | --- | --- |
| 方法一：克隆源码 | 需要 Git 和 Conda | 否 |
| 方法二：安装包 | 不需要 Python/Conda | 是 |

### 方法一：克隆源码并创建环境

**前置条件**

- Windows 10/11 x64
- [Git](https://git-scm.com/download/win)
- Miniconda 或 Anaconda
- 首次安装 Python 依赖时能够访问软件包源

在 PowerShell 中依次执行：

```powershell
# 1. 下载完整源码。
git clone https://github.com/Handkerchief-tj/ISACA-Desktop.git

# 2. 进入仓库根目录。
cd ISACA-Desktop

# 3. 按 environment.yml 创建独立的 Python 3.12 环境并安装依赖。
conda env create -f environment.yml

# 4. 激活刚创建的环境。
conda activate isaca_desktop

# 5. 快速检查 Python、SLiCAP、PySide6、SFG 和依赖一致性。
./scripts/check-environment.ps1 -SkipTests

# 6. 启动桌面程序。
./start-desktop.ps1
```

SLiCAP 第一次导入时可能询问 `Do you have NGspice installed?`。如果没有安装
NGspice，输入 `n` 即可；ISACA 当前的原理图、SLiCAP 符号分析和 SFG 主流程不依赖
NGspice。该选择保存在用户目录的 `~/SLiCAP.ini` 中。

需要运行完整桌面与算法测试时执行：

```powershell
# 不加 -SkipTests 会运行全部回归测试。
./scripts/check-environment.ps1
```

需要更新源码时执行：

```powershell
# 拉取 main 的最新提交。
git pull

# 更新 editable 安装，并重新检查依赖；不会删除用户项目。
./setup.ps1 -SkipTests
```

更完整的源码安装和故障排查见
[源码环境与验收说明](docs/desktop/teammate-setup.md)。

### 方法二：下载 Windows 安装包

安装包适合不需要修改源码的教师和学生，内部已经携带 Python 3.12、SLiCAP 5.2.1、
PySide6、SFG 算法和私有 Graphviz。

1. 打开 [ISACA Desktop Releases](https://github.com/Handkerchief-tj/ISACA-Desktop/releases/latest)。
2. 在最新版本的 **Assets** 中下载 `ISACA-Desktop-<版本>-win64-setup.exe`。
3. 同时下载 `release-manifest-<版本>.json`，需要时可按其中的 SHA-256 校验安装包。
4. 双击安装器，根据提示安装，然后从开始菜单启动 `ISACA Desktop`。

```powershell
# 可选：在安装前计算文件 SHA-256，并与 release manifest 对照。
Get-FileHash ./ISACA-Desktop-<版本>-win64-setup.exe -Algorithm SHA256
```

安装采用当前用户模式，不需要管理员权限，默认安装到
`%LOCALAPPDATA%\Programs\ISACA`，也不会修改系统 Python 或 `PATH`。当前安装包尚未
进行商业代码签名，Windows SmartScreen 可能显示“未知发布者”。


## 基本使用流程

1. **创建项目**：在左侧“项目与输入”面板新建项目，或打开已有 SLiCAP 项目目录。
2. **输入电路**：使用中央官方画布创建/打开 `.slicap_sch`；也可以点击“新建当前项目的网表”
   或“打开并编辑网表”，随后在中央网表编辑器中直接修改 `.cir`。
3. **设置分析端口**：填写器件参数和 `.param`，确认 source、detector。
4. **生成并检查网表**：原理图由 SLiCAP 官方导出器生成 `.cir`；手写网表可以直接
   验证和保存。
5. **选择分析**：在右侧选择传递函数、极零点、MNA、Bode、噪声和 SFG 分频段化简，
   必要时填写频率范围及误差限制。
6. **查看结果**：在结果标签中查看公式、数值根、矩阵、频率图、根聚类和逐频段符号
   表达式；生成文件保存在项目的 `runs/` 目录。

详细操作见 [桌面版使用说明](docs/desktop/usage.md)。仓库中的
`examples/desktop/rc_lowpass.cir` 和 `examples/desktop/demo_2_numeric.cir` 可用于首次
测试。

## 目录结构

```text
ISACA-Desktop/
├─ src/
│  ├─ isaca_desktop/     PySide6 主界面、SLiCAP 画布壳层、worker、结果展示
│  ├─ isaca_api/         数据模型、参数系统、网表处理、SLiCAP 5.2.1 适配层
│  └─ sfg_prototype/     SFG 构图、化简、根定位和符号恢复算法
├─ tests/
│  ├─ desktop/           桌面、网表、参数、worker 和 SLiCAP 集成测试
│  └─ algorithm/         SFG 算法、受控源和论文示例测试
├─ examples/             RC 低通和 demo_2 等可运行电路
├─ docs/                 使用说明、算法说明、架构与迁移记录
├─ scripts/              环境检查、分析验证和 Windows 发布脚本
├─ packaging/            Inno Setup 配置、发布依赖约束和第三方声明
├─ environment.yml       Conda 源码开发环境
├─ pyproject.toml        Python 包、运行依赖和可选依赖
├─ setup.ps1             源码安装与自检入口
└─ start-desktop.ps1     桌面开发版启动入口
```

`build/`、`deployment/`、`dist/`、Conda 环境、运行结果、模型权重和安装包均由
`.gitignore` 排除，不会进入源码仓库。

## 当前范围

- 支持 Windows 10/11 x64，固定使用 Python 3.12 和 SLiCAP 5.2.1。
- 第一版不包含大模型辅助报告，也不分发 netLens 视觉模型及权重。
- SFG 化简仍属于科研原型；对于高阶、强耦合或异常拓扑，局部根可能得到较长表达式
  或明确的 `UNRESOLVED`，不应描述为能对任意电路稳定产生最简解析式。

算法细节与论文对齐记录见 [SFG 算法说明](docs/algorithm/README.md)。
