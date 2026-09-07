# ISACA 桌面开发版使用说明

## 环境与入口

固定 Python 3.12、SLiCAP 5.2.1，SFG 算法源码随本仓库统一安装。
不修改旧 `slicap_env`，不修改任何 Conda 环境中的 SLiCAP 源码。

在 `ISACA-Desktop` 仓库根目录：

```powershell
conda activate isaca_desktop
.\start-desktop.ps1
```

可选参数 `-Project <项目目录>` 和 `-File <.cir 或 .slicap_sch 文件>`。
启动器检查 Python、SLiCAP、QtWebEngine 和 SFG 包，不会启动网络服务。
本地公式资源已经随源码保存，无需访问 CDN。

## 官方原理图

1. 左侧创建/打开一个项目文件夹。建议使用独立用户项目，不把安装目录作为项目。
2. 点击“新建原理图”，或打开已有 `.slicap_sch`。
3. 在官方画布绘制、连线、设置元件参数、`.param`、source 和 detector。绘图菜单仍是官方英文菜单。
4. 保存原理图；右侧选择输入为原理图，再运行分析，或使用菜单“导出当前原理图并分析”。
5. 程序保存当前图后，在独立 worker 中调用 SLiCAP 官方 CLI 使用的加载器和导出函数。
6. 生成的 `.cir` 进入严格参数检查，数值完整后执行分析。

器件参数和电路参数不是同一层概念。器件 `Properties` 中的 `Q1.gm=gm_Q1` 表示该器件字段引用符号
`gm_Q1`；`Place > Parameters` 中的 `gm_Q1=40m` 才是该符号的电路级数值定义。ISACA 增强后的
Parameter 单元格会列出当前器件属性实际引用的符号，并显示 `Q1.gm` 等来源；同一个符号由多个器件
共用时只需定义一次。表格支持单击编辑以及方向键切换单元格。

器件属性留空时，官方导出器会省略该字段，随后 SLiCAP 按器件模型补默认值，而不是统一补 0 或 1。
例如 SLiCAP 5.2.1 的 `QV` 默认 `gm=40m`、`gpi=2.5k`，其余多数小信号参数为 0。若需要可复现的
具体器件值，应在 Properties 中显式填写数值，或填写符号后通过 Parameters 定义。

原理图是绘图权威文件，导出的 `.cir` 是分析输入。选择原理图分析时总是重新导出，
不会误用上一次留在网表编辑器里的旧网表。取消保存时不启动导出。

## 手写网表

左侧“打开网表”可载入 `examples/desktop/rc_lowpass.cir` 或 `demo_2_numeric.cir`。
网表编辑器支持行号、基础语法高亮、保存和未保存修改提示。
右侧输入模式选择网表，点“验证”查看参数来源与缺失项。

参数表中的数值可以覆盖，但只把真正修改过的单元格作为用户覆盖；
未修改项保持原有 `.param` 或 inline 来源。覆盖参数后会重新解析依赖表达式。
缺失参数不自动设为 1。“使用 SLiCAP 默认值”是显式选项，不代表实际工艺参数。

## 分析设置与结果

可选择 Laplace、PZ、Matrix、Noise、Bode 和 SFG 符号化简。
默认幅值误差 2 dB、相位误差 5°，每个频段最多接受 10 个操作。
两个频率输入框同时留空时由分析流程按根自动外推范围；填写时单位为 Hz，要求有限、正值、递增。
复现论文单管示例时应显式填写 `10` 和 `1e11` Hz。
同时应核对 `cpi=12.5n`；`12.5f` 比论文值小 `10^6` 倍，会显著改变极点、根簇和频段符号式。
SFG 根聚类和误差受控化简必须提供完整数值，即使其输出是符号表达式。

结果标签展示分析摘要、展平元件、数值传函及极零点、MNA、噪声、SFG 分频段结果和文件列表。
MNA 页使用标准矩阵方程显示；SFG 页使用十倍频程对数轴标出闭环根、root cluster 和频率子区间。
数值页首先给出由极点、零点与低频增益重写的可读传递函数，同时保留 SLiCAP 原始精确式；`H(0)`
明确标为低频小信号增益。零极点先列 Hz，再保留 s 平面 rad/s。SFG 页按根聚类频段图、每频段传函、
符号根、主导参数和误差验收的论文逻辑展示。
根表达式使用 s 平面约定，根的频率另以 Hz 显示。零点的左右半平面符号不得丢失。
子图传函的频响误差和局部根表达式的根位置偏差分别展示。默认论文模式只使用前者
验收图操作；后者用于说明物理短式与精确闭环根之间发生了多大 root shifting，
不再显示为独立的“通过/失败”。局部根解释不等于全图精确求根。
完整 ranking、root localization 和 error trace 报告可在“生成文件”中打开。

计算通过 QProcess 运行。点取消会写取消请求，超时后仅结束当前 worker；不会删除项目。
每个任务的 request.json、worker-result.json、日志和 desktop-state.json 保存在项目 runs 子目录。
计算途中关闭窗口会询问是否取消任务，未保存的图和网表会单独询问。

## Graphviz

开发版暂时可以使用当前环境的 dot，或仅给本次进程设置：

```powershell
$env:ISACA_GRAPHVIZ_DOT = '你的 Graphviz 路径\dot.exe'
.\start-desktop.ps1
```

这不修改系统 PATH。dot 缺失或超时只会影响 SFG SVG，不会丢弃 DOT、表达式和数值结果。
发布版的私有 Graphviz 尚待构建及验收，不要把开发机可运行当成安装包已完成。

## 暂缓的功能

视觉入口目前仅为实验性接口，按用户决定暂停视觉测试和性能验证，不纳入本阶段验收。
不要依赖其识别结果直接分析陌生电路。CPU/GPU 包、模型分发、故障回退仍需独立测试。
本版没有大模型辅助报告，没有云端部署，也没有已验收的 EXE 安装器。

## 可重复验证

```powershell
.\scripts\check-environment.ps1
python scripts/verify-desktop-analysis.py --case rc_lowpass --output runs/verify-rc-01
python scripts/verify-desktop-analysis.py --case demo_2_numeric --symbolic --output runs/verify-demo2-01
```

输出目录必须是新的目录，以免覆盖前次证据。脚本复用桌面 worker，不另写数值/化简算法。
统一检查命令会同时运行 `tests/desktop` 与 `tests/algorithm`，无需切换仓库。
