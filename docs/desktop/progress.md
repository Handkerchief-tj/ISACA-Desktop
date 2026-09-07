# 桌面第一阶段实施记录

日期：2026-09-07。最新范围调整：建立独立的单仓库桌面版，视觉模块暂缓测试。

## 冻结基线

- 产品仓库：`ISACA-Desktop` 只保留桌面程序、分析核心、算法、测试和文档；已弃用的
  React Web Schematic 与旧项目历史不进入新仓库。
- 算法源码：原 `sfg-prototype 0.2.3` 已并入 `src/sfg_prototype`，与产品统一版本，
  但仍通过公开 API 与桌面界面解耦。
- 开发环境：Python 3.12、SLiCAP 5.2.1；旧 SLiCAP 4.0.8 环境保持不动。
- 算法回归：52 项通过，0 failure/0 error/0 skip，JUnit 记录在 `runs/desktop-sfg-regression.xml`。

## 已实现的里程碑

| 里程碑 | 状态与证据范围 |
|---|---|
| 官方桌面壳层 | 已实现；直接复用 MainWindow/CanvasPanel，RC 实际实例化、保存和导出测试通过 |
| 项目与输入 | 已实现项目目录、网表编辑器、参数表和输入模式切换；不是任意网表自动反向布局 |
| 官方导出 | 在隔离 worker 中调用官方 CLI 的 `_load_scene/_write_netlist`，避免冻结 EXE 后 `python -m` 无效 |
| 数值 worker | 已接入 Laplace/PZ/Matrix/Noise/Bode，结果直接来自对象，不解析 SLiCAP HTML |
| SFG worker | 调用仓库内置算法包，返回频段传函、目标根解释、参数、误差与报告路径 |
| 任务生命周期 | queued/running/completed/failed/cancelled；请求与结果持久化；取消后可再启动 |
| 离线结果 | KaTeX 0.16.22 与字体、许可证入库；大结果使用本地文件页避免 Qt setHtml 2 MB 上限 |
| 视觉 | 接口原型保留；用户要求暂停测试，不标记为可发布功能 |
| 安装发行版 | 尚未完成；standalone、Graphviz 分发与干净 Windows 安装/卸载测试仍待进行 |

旧集成仓库曾构建并检查开发 wheel `isaca_local-0.1.0-py3-none-any.whl`。新单仓库将重新
构建统一 `isaca-desktop` 制品；无论哪种 wheel 都只是 Python 包，不是独立运行的 EXE。

## 本轮修复的可靠性问题

1. 官方 CanvasPanel 的 `_scene` 和 `_current_path` 是实例属性，不能在类上检查；修正版本适配检查。
2. 原理图修改后不能直接分析旧网表；现在原理图模式强制先保存并导出。
3. 参数表不能把每次展示都重新解释为“用户覆盖”；只记录实际变动，并重新求值依赖参数。
4. QProcess 取消定时器不能跨任务存活，否则可能误杀后续 worker；改成每任务拥有并及时停止。
5. 保持 QApplication 的强引用，避免官方网表导出测试中 Qt 对象提前释放造成进程崩溃。
6. NumPy 根数组不能用 `array or []` 判断；显式处理 None。
7. Graphviz 缺失/超时不应使已经成功的符号计算消失；保留结果并增加 warning。
8. 分频段误差与全局误差分开序列化；GUI 不在主线程重新解析大型 SymPy 表达式。

## 验证记录

核心回归与 demo 的最新数值以对应 JUnit/verification.json 为准，不以历史聊天中的计数替代。
当前后端/桌面回归为 **38 passed / 1 skipped**，已覆盖参数候选、方向键、可读传函、Hz 根表与频段图；
跳过项仍仅为用户暂停的视觉校对测试。
当前 `slicap-5.2-integration` 算法分支扩展后为 **60 passed**。
RC 在 offscreen 与 Windows 原生 Qt 平台均完成保存、官方网表导出、取消后重启、数值及 Bode 分析；
原生窗口检查到 2 个实际 KaTeX 公式，极点为 -1000 rad/s。
最后一轮新增导出输入哈希检查、运行中输入锁定和持久化日志后，再次通过完整回归。
首次新增视觉校对测试使用了错误的 HSPICE 模型标记，出现一次夹具失败；已改为 netLens 格式，
但按用户最新要求保持显式跳过，不声称该视觉测试已经重新通过。

### demo_2 的当前结果，不等同于全面复现验收

桌面 worker 首次完整验证耗时约 217 秒（与后端回归同时运行，非独立性能基准），接受 21 个操作。
参考根为 3 个极点、2 个零点。频率边界来自本次配置的算法流程，不手工替换成论文图注边界。

| Cluster | 频段 / Hz | 最大幅值误差 / dB | 最大相位误差 / deg |
|---|---|---|---|
| 1 | 1.000000e1 – 3.399850e3 | 3.362558e-4 | 7.882563e-3 |
| 2 | 3.399850e3 – 4.685967e5 | 5.884220e-2 | 4.194397 |
| 3 | 4.685967e5 – 9.823256e7 | 8.942093e-2 | 4.266253 |
| 4 | 9.823256e7 – 1.000000e11 | 8.964611e-2 | 3.697497 |

四个子图在算法的频率采样检查中均处于 ±2 dB、±5° 范围内。恢复了 `gm/cmu`、
Eq.(24) `-cmu*gx/(cx*(cmu+cpi))` 和输入极点 `-(Gin+gx)/cx` 的符号形式。
第二频段现在保留论文 Eq. (22) 的求和点极点，约为 `-47.7 kHz`。它相对精确闭环
极点约 `-34.6 kHz` 的根位置偏差约为 `36.9%`，但该频段化简图的最大传函误差约为
`0.059 dB`、`4.19 deg`，满足用户设置的 `±2 dB`、`±5 deg`。因此默认论文模式将
该根标为“已定位”，而不是用额外 5% 逐根门槛否决它。代码不再回到原始 SFG 为
满足根误差而重选表达式；逐根位置限制仅作为用户显式开启的可选扩展。
5 个目标根均获得符号解释，Eq. (22) 和 Eq. (24) 均通过 SymPy 符号等价断言。
小图默认先枚举完整的 `RSP/RR` 候选，再由论文 Eq. (6)-(7) 与 Eq. (11)-(12)
决定接受和排序；`demo_2_numeric` 的 meta-edge 数低于自动上限，因此不再使用固定贡献阈值预先丢弃候选。
开环极点的反馈遮蔽边界也已改为论文所述的 `|loop gain| >= 1`，并写入验证配置。
当前唯一 warning 是尚未配置私有 Graphviz，不影响数值与符号计算。精简且可入库的结构化证据见
`verification-2026-09-07.json`；本次完整本地证据位于临时验证目录的
`isaca-demo2-paper-default-final-20260907/verification.json`。

单仓库整理后的重新验收为 **98 passed / 1 skipped**；RC 与 `demo_2_numeric` 均通过
同一桌面 worker 入口，完整证据摘要见 [单仓库基线记录](monorepo-baseline-2026-09-07.md)。

## 下一阶段与验收边界

1. 扩大官方画布电路回归到 RLC、MOS/BJT、四种受控源、浮置源和子电路，并做实际 GUI 往返。
2. 检查多项目切换、任务异常退出后恢复、噪声显示和复杂表达式的大结果体验。
3. 构建统一 wheel，固定编译工具版本，准备 pyside6-deploy standalone；不能只写 spec 就称打包完成。
4. 审计 Qt/SLiCAP/SFG/KaTeX/Graphviz 的资源与许可证，确保未混入 Web、模型权重或开发缓存。
5. 私有 Graphviz 打包后，在无 Python/Conda/全局 Graphviz 的干净 Windows 上运行 RC、demo_2、取消恢复和卸载测试。
6. 视觉验收等用户恢复此范围后再单独推进。本轮不加载视觉模型。

官方接口参考：[SLiCAP GUI](https://www.slicap.org/GUI/)、
[Qt deployment](https://doc.qt.io/qtforpython-6/deployment/deployment-pyside6-deploy.html)、
[KaTeX browser usage](https://katex.org/docs/browser.html)。实际适配以本机固定的 5.2.1 源码为准。
