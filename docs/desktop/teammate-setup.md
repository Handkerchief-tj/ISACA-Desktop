# 队友安装与验收说明

本文用于在新的 Windows 10/11 开发机上运行 ISACA 桌面开发版。桌面界面、
SLiCAP 适配层和 SFG 算法都在 `ISACA-Desktop` 一个仓库中，不需要第二个仓库。

## 交付范围

- 绘图界面直接复用 SLiCAP 5.2.1 官方 Structured Electronic Design Environment。
- 数值分析包括 Laplace、PZ、Matrix、Noise 和 Bode。
- 仓库内置完整 `sfg_prototype` 源码，提供分频段符号化简。
- 旧 React Web Schematic、视觉模型和大模型报告不属于当前验收。
- 正确入口是 `start-desktop.ps1`，不需要 Node.js，也不访问本地网页端口。
- 当前是源码开发版，不是独立 EXE 安装包。

## 1. 克隆唯一仓库

```powershell
git clone https://github.com/Handkerchief-tj/ISACA-Desktop.git
cd ISACA-Desktop
```

## 2. 创建固定环境

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

### Graphviz 警告

Graphviz 只用于把 DOT 渲染为 SVG。缺失时传递函数、极零点和符号结果仍会保留，
只是部分 SFG 图像无法生成。正式安装包将携带私有 Graphviz。

### 为什么没有网页地址

这是桌面开发版。旧 Web Schematic 已从交付中移除；启动命令直接打开 Qt 窗口。

## 更新代码

```powershell
cd <ISACA-Desktop 路径>
git pull
.\setup.ps1
```

这一个更新流程会同时更新桌面端和算法端。
