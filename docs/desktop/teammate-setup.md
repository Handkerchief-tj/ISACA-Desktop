# 源码环境与验收说明

本文用于在新的 Windows 10/11 开发机上运行 ISACA 桌面开发版。桌面界面、
SLiCAP 适配层和 SFG 算法都在 `ISACA-Desktop` 一个仓库中，不需要第二个仓库。

## 交付范围

- 绘图界面直接复用 SLiCAP 5.2.1 官方 Structured Electronic Design Environment。
- 数值分析包括 Laplace、PZ、Matrix、Noise 和 Bode。
- 仓库内置完整 `sfg_prototype` 源码，提供分频段符号化简。
- 旧 React Web Schematic、视觉模型和大模型报告不属于当前验收。
- 正确入口是 `start-desktop.ps1`，不需要 Node.js，也不访问本地网页端口。
- 本文描述源码开发方式；构建出的 EXE、安装包和本地环境不会提交到源码仓库。

## 0. 前置条件

- Windows 10/11 x64。
- Git。
- Miniconda 或 Anaconda；不要直接改系统 Python 或 Conda `base` 环境。
- 首次安装依赖时可以访问 Python 包源。
- Graphviz 仅用于生成 SFG SVG，源码开发时可选，不影响数值和符号结果本身。

## 1. 克隆唯一仓库

```powershell
git clone https://github.com/Handkerchief-tj/ISACA-Desktop.git
cd ISACA-Desktop
```

## 2. 创建固定环境

推荐让 Conda 读取仓库中的环境文件：

```powershell
conda env create -f environment.yml
conda activate isaca_desktop
.\scripts\check-environment.ps1 -SkipTests
```

如果已经存在同名环境，使用下面的手动方式，不要反复执行 `conda env create`：

```powershell
conda create -n isaca_desktop python=3.12 -y
conda activate isaca_desktop
.\setup.ps1
```

`setup.ps1` 会安装 `SLiCAP==5.2.1`、PySide6、桌面依赖和当前仓库中的三组
Python 包，然后统一运行桌面与算法测试。不要在旧的 SLiCAP 4.0.8 环境中覆盖安装。

只想先安装、不立即运行测试时可以执行：

```powershell
.\setup.ps1 -SkipTests
```

此时仍会执行版本、关键模块导入和 `pip check`。只有要在本机重新构建 Windows
安装包时才执行 `.\setup.ps1 -WithDeploy`；普通运行和算法开发不需要发布依赖。

首次导入 SLiCAP 时可能出现 `Do you have NGspice installed? [y/n]`。没有安装
NGspice 时输入 `n`；ISACA 当前的官方原理图、SLiCAP 符号分析和 SFG 主流程不依赖
它。该选择保存在用户目录的 `~/SLiCAP.ini`，不会写入 Git 仓库。

依赖来源只有三处：

- `pyproject.toml`：运行、测试和可选发布依赖的版本范围。
- `environment.yml`：协作者默认 Conda 环境入口，安装 `.[test]`。
- `packaging/constraints-release.txt`：已验证发布构建的精确版本，不是第二套源码。

## 3. 验证环境

```powershell
$python = (Get-Command python.exe).Source
.\scripts\check-environment.ps1 -Python $python
```

输出应显示 Python 3.12、SLiCAP 5.2.1、PySide6，以及位于本仓库 `src` 中的
`sfg_prototype` 路径，随后执行全部测试。视觉测试按当前范围保持跳过。

## 4. 启动桌面程序

```powershell
.\start-desktop.ps1
```

预期结果是直接打开 PySide6 桌面窗口，不会显示 5173、8000 或 7860 端口。

也可以直接打开项目或电路文件：

```powershell
.\start-desktop.ps1 -Project "C:\path\to\project"
.\start-desktop.ps1 -File ".\examples\desktop\demo_2_numeric.cir"
```

## 5. 最小验收流程

1. 打开 `examples/desktop/rc_lowpass.cir`，执行 Laplace、PZ、Matrix 和 Bode。
2. 确认结果页显示可读传递函数、Hz/rad/s 极点和矩阵形式的 MNA。
3. 打开 `examples/desktop/demo_2_numeric.cir`，频率范围设为 `10` 到 `1e11` Hz。
4. 执行 SFG 分析，确认出现 4 个 root cluster、频率子区间和逐频段符号结果。
5. 新建官方 schematic，绘制 RC 低通，设置 source/detector，保存并导出分析。

## 常见问题

### `No module named sfg_prototype`

通常是没有在仓库根目录执行 `setup.ps1`，或者启动时激活了另一个 Conda 环境。
不需要下载算法仓库；重新激活 `isaca_desktop` 并执行安装脚本即可。

### `SLiCAP==5.2.1 required`

当前终端没有激活正确环境。执行 `conda activate isaca_desktop`，再检查：

```powershell
python -c "import SLiCAP; print(SLiCAP.__version__)"
```

### `pip check` 报告无关包冲突

说明当前环境还混装了其他项目依赖。最可靠的做法是退出该环境，按本文用
`environment.yml` 新建独立的 `isaca_desktop` 环境，而不是在 Conda `base` 或旧的
视觉/网页环境中继续覆盖安装。

### Graphviz 警告

Graphviz 只用于把 DOT 渲染为 SVG。缺失时传递函数、极零点和符号结果仍会保留，
只是部分 SFG 图像无法生成。安装包构建流程会携带私有 Graphviz。

### PowerShell 禁止执行脚本

不需要永久修改系统策略。可以只对当前 PowerShell 进程临时允许：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

然后重新运行 `.\setup.ps1` 或 `.\start-desktop.ps1`。

### 更新后依赖发生变化

拉取代码后重新执行 `.\setup.ps1 -SkipTests`。这是可重复操作，会更新当前仓库的
editable 安装并重新检查依赖，不需要删除项目文件。

### 为什么没有网页地址

这是桌面开发版。旧 Web Schematic 已从交付中移除；启动命令直接打开 Qt 窗口。

## 更新代码

```powershell
cd <ISACA-Desktop 路径>
git pull
.\setup.ps1 -SkipTests
```

这一个更新流程会同时更新桌面端和算法端。
