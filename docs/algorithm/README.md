# 信号流图符号化简原型

本目录独立实现论文 *Circuit Simplification for the Symbolic Analysis of Analog Integrated Circuits* 的主要算法框架。SLiCAP 负责网表解析、层次展平和原始电路参考求解；本模块负责信号流图构建、根聚类、误差受控图操作、根定位和频段符号结果。

```text
.cir -> SLiCAP 展平网络 N -> 信号流图 G
     -> 参考 H(s)/闭环根 -> root clustering / frequency subranges
     -> RSP/RR/VPC/SS 误差受控化简 -> G_j^*
     -> 频段传递函数、零极点与根来源报告
```

## 版本定位

当前版本是可运行、可测试的科研原型，而不是已经覆盖任意模拟电路的最终符号化简器。
系统已经贯通论文 Fig. 12 的数据流，并能在小型和中型网表上执行数值参考求解、根聚类、
频段划分、图操作、误差验收和局部根表达式提取。当前版本还能够从求和点的多条
前向路径构造局部 Mason 分子，并在路径分析受限时使用局部系统矩阵回退，从而解释
不能归因于单条 `meta-edge` 的闭环零点。逐根表达式尚未在所有大型电路和频段稳定
重构为最终近似传递函数。

| 论文阶段 | 当前状态 | 实现内容与边界 |
|---|---|---|
| 1-3：`N -> G` | 已实现，作为稳定基线 | SLiCAP 解析与展平、SFG stamp、电源一致性、冗余删除、自环吸收和 `meta-edge` 合并 |
| 4：参考性能与根 | 已实现 | 可选 SLiCAP 符号参考、数值 SFG 或 descriptor-MNA 广义特征值/Rosenbrock 零点 |
| 5-6：根聚类与误差子区间 | 已实现 | 按 Eq. (13)-(14) 自动形成 root cluster 和 frequency subrange |
| 7：误差受控预化简 | 基础实现 | 仅对用户声明的偏置节点合并使用独立小误差预算，不擅自删除信号结构 |
| 8：拓扑分析 | 已实现 | forward path、feedback loop、summing vertex、strict/fallback graph cut 和复杂度指标 |
| 9.1：局部子图 | 已实现 | 为每个根簇构造并保存频段图 `G_j` |
| 9.2：图操作与 ranking | 框架已实现，仍在严格化 | 小图完整枚举 `RSP/RR`，大图有界预筛选；支持 `VPC/SS`、apply/rollback、`QP/EP/RP` 和幅相误差验收 |
| 9.3：根定位 | 主要框架已实现 | 可定位 `OLR-O`、`OLR-NO`、`SPR-P` 和 `SPR-Z`；超出局部路径/矩阵规模限制时仍会明确返回 `unresolved` |
| 最终符号结果 | 部分完成 | 输出精确频段图传函、局部逐根短式及参数影响；完整的逐根重构与统一频响验收仍待完成 |

根聚类使用“整个簇的跨度”而不是相邻根链式合并：一个新根只有在它与当前簇最低频根
仍满足论文相对距离阈值时才进入该簇。这样可以避免多个两两接近、但首尾相距很远的根
被错误并成一个频段。

### 根分类术语

- `OLR-O`：observable open-loop root。根来自单条 `meta-edge` 的局部传递因子，并且从该结构到检测器存在未被其他路径压制的可观测传播路径。
- `OLR-NO`：non-observable open-loop root。根虽然存在于某条 `meta-edge`，但在闭环传递中被反馈、旁路或极零相消遮蔽，不能直接作为输出闭环根解释。
- `SPR-P`：summing-point pole。由求和点附近反馈回路的局部 Mason 特征式产生的极点。
- `SPR-Z`：summing-point zero。由多条有符号前向路径在求和点抵消或接管产生的零点。
- `UNRESOLVED`：数值闭环根已经存在并已聚类，但当前局部 OLR/SPR 模型尚未找到可解释的符号候选。

## 当前能力

- 读取 SLiCAP `.cir` 网表，保留 `.param` 数值替换并得到展平小信号网络 `N`。
- 构建符号 SFG，支持独立源、四类受控源、浮置电压源辅助电流、电压源链方向审计、冗余删除、自环吸收和 meta-edge 合并。
- 用未化简 SFG 与 SLiCAP 原生传递函数做硬等价性检查；基线不一致时拒绝进入化简。
- 提供实验性的数值 MNA 描述矩阵接口：从 `M(s)=G+sC` 直接计算广义特征值和 Rosenbrock 传输零点，避免先展开高阶符号行列式。
- 按论文 Eq. (13)-(14) 对闭环零极点聚类，并与用户频率范围相交生成误差子区间。
- 按 Eq. (6)-(7) 在频率网格上计算幅值、相位或复相对误差；累计绝对误差决定接受，相对上一步的误差用于 Eq. (11) ranking。
- 支持 Eq. (9)、(10) 图复杂度，以及 Eq. (11)、(12) 的操作质量和排序值。
- 实现 `RSP`、`RR`、`VPC`、`SS` 四类图操作；每次操作作用于图副本，可拒绝并回退。
- `candidate_generation_mode="auto"` 在不超过 16 条 meta-edge 的小图中先枚举全部合法 `RSP/RR` 候选，再由 Eq. (6)-(7) 与 Eq. (11)-(12) 取舍；更大图才启用固定阈值预筛选。
- 区分可观测开环根 `OLR-O`、不可观测开环根 `OLR-NO` 和求和点根 `SPR`。
- 开环极点的反馈遮蔽默认以论文的 `|loop gain| >= 1` 为边界；下游竞争路径的“完全支配”比例因论文未给数值，作为显式配置和报告项保留。
- 从每个最终频段图 `G_j^*` 直接求符号传递函数与精确图根。
- 从局部反馈 SCC 自动形成 Mason 特征式、选择主导回路并提取 SPR；随后按设计点和参数角点双重误差约束删除非主导乘积项，保留完整式、主导回路式和最终短式三层结果。
- 对尚未由单条 meta-edge 解释的闭环零点，在真实复频率 `s=z*` 比较求和点输入贡献，构造 `N_cut(s)=sum(P_k*Delta_k)` 并提取 `SPR-Z`；路径数量或表达式规模超限时回退到局部 Rosenbrock-style 系统矩阵。
- strict complementary graph cut 可以生成保守的 `SS` 等效子图替代候选；fallback/reconvergent 区域只用于根解释，不会被自动替换。
- 对每个频段中的每个闭环目标根统一生成 OLR/SPR 局部符号候选，并输出定位状态、根位置偏差、根所在边/求和点以及表达式中的主导参数。
- 默认采用论文原型的虚轴传函误差模式：图操作由 Eq. (6)-(7) 的子频段性能误差接受或拒绝，根位置偏差只作诊断；不会为满足逐根误差门槛而回到原始 SFG 重选表达式。

### 当前验证结果

- 完整自动化测试覆盖网表展平、特殊电源、受控源、SFG 等价性、descriptor-MNA、根聚类、拓扑分析、四类图操作、SPR-P 和 SPR-Z。
- 论文单管放大器 `demo_2` 的 3 个极点和 2 个零点被自动分成 4 个根簇；当前能够为全部 5 个闭环根给出局部符号解释。
- `gm/cmu` 零点被识别为 `OLR-O`；两个低频极点被识别为 `SPR`；一个高频极点被识别为 `OLR-O`。
- 第一频段极点已自动重构为紧凑的重复参数组。令 `A=Gin*gpi+Gin*gx+gpi*gx`，结果等价于 `-(Gl+go)*A / ((Cl+cmu+cx)*A + cmu*gm*(Gin+gx))`，约 `-346.01 Hz`；它是当前 Fig.11(a) 化简图的一阶传函精确极点，而论文 Eq.(21) 还包含一层未公开判据的近似。
- 第二频段保留论文 Eq. (22) 的求和点极点，当前参数下约为 `-47.7 kHz`；它相对精确闭环根约 `-34.6 kHz` 的位置偏差约为 `36.9%`。这只作为诊断，因为该频段化简图误差约为 `0.059 dB`、`4.19 deg`，仍满足用户设置的 `±2 dB`、`±5 deg`。
- 高频负零点被识别为 `SPR-Z`。程序自动得到两条局部路径 `s*cx` 和 `gx*cmu/(cmu+cpi)`，恢复论文 Eq. (24)：`-gx*cmu/(cx*(cmu+cpi))`；相对完整闭环数值零点误差约为 `0.418%`。
- 高阶公开 BJT 网表可通过 descriptor-MNA 快速求数值根，但手工转录网表与论文参考数据仍有低中频差异，因此尚未声明完成该算例的论文数值复现。

按 Table II 闭环根直接应用 Eq. (13)-(14) 时，内部频段边界约为 `3.40 kHz`、
`468.6 kHz`、`98.2 MHz`；论文 Table III 的前两项是 `5.79 kHz`、`549 kHz`。
正文没有给出足以重现这两个差异值的额外 root-set 规则，因此程序不硬编码论文边界，
并在一致性审计中将其列为待澄清问题。

根位置偏差和频响误差是两个不同的量。论文原型在虚轴频率网格上控制整个 `H_j(s)` 的
幅值和相位，因此允许 root shifting；局部根式与精确闭环根的距离只用于解释近似程度，
不重复充当默认验收门槛。逐根位置控制作为可选扩展保留。

更完整的逐项对照见 [论文算法一致性审计](paper_algorithm_alignment.md)。

## 代码结构

```text
ISACA-Desktop/
  src/sfg_prototype/
    network.py      # SLiCAP circuit -> 展平线性网络 N、数值参数表
    sfg.py          # N -> 基础信号流图 G、审计与 Graphviz 导出
    analysis.py     # 参考求解、根聚类、拓扑、graph cut、OLR/SPR 定位
    simplify.py     # RSP、RR、VPC、SS 的候选与 apply
    pipeline.py     # Fig. 12 顺序、误差控制、ranking、G_j^* 和报告
  tests/algorithm/
    fixtures/       # RC、电源、受控源和论文单管例子网表
  scripts/algorithm/
    run_demo.py
  examples/algorithm/demo_2/
```

## 环境安装

算法已经并入 ISACA Desktop，无需单独安装。请在仓库根目录执行：

```powershell
conda activate isaca_desktop
.\setup.ps1
```

确认 Graphviz：

```powershell
python -c "import graphviz; print(graphviz.__version__)"
dot -V
```

本项目调用了部分 SLiCAP 内部接口，升级 SLiCAP 后必须重新运行测试。

完整化简期间，程序会在单次 `simplify_graph()` 调用内部缓存相同图状态的根候选、
forward path/feedback loop/graph cut、论文复杂度和数值频响。缓存不会跨网表或跨进程
持久化，图操作改变任何边表达式后会自动使用新键重新分析，因此只减少重复计算，
不会改变 ranking 或误差验收结果。

## 基础用法

```python
from sfg_prototype import SimplificationConfig, simplify_netlist

config = SimplificationConfig(
    cluster_tolerance=0.9,
    frequency_range_hz=(10.0, 1.0e11),
    magnitude_error_db=2.0,
    phase_error_deg=5.0,
    max_steps_per_subrange=10,
    local_spr_relative_frequency_error=0.5,
    local_spr_term_relative_frequency_error=0.05,
    local_spr_term_corner_relative_error=0.05,
    enable_transfer_term_pruning=True,
    transfer_term_max_steps=12,
    enforce_target_root_error=False,
    target_root_relative_error=0.05,
    enable_local_zero_expressions=True,
    local_zero_strategy="auto",
)
result = simplify_netlist(r"path\to\circuit.cir", config=config)

for item in result.subrange_results:
    print(item.cluster_index, item.transfer.transfer)
    print(item.transfer.poles, item.transfer.zeros)
    print(item.target_root_approximations)
```

`enforce_target_root_error=False` 是论文兼容模式。此时 `target_root_relative_error` 不参与
默认接受/拒绝，系统仍会输出 `relative_root_error` 供诊断。只有显式设置
`enforce_target_root_error=True` 时，才启用论文 Section III-E 提到的“按极零点位置控制”
扩展，并用 `target_root_relative_error` 标记 `outside_error_limit`。

默认直接从最终频段图 `G_j^*` 定位根，不再为满足逐根误差或缩短表达式而回到原始
SFG。`target_root_use_frequency_reduction=True` 仅作为实验选项：它会先检查 `G_j^*`
的额外主导项视图；如果完全找不到候选，也只回到同一个 `G_j^*`。这一选项不参与
图操作的接受判定。

`local_zero_strategy="auto"` 先使用物理意义最清楚的局部 Mason 路径分子；路径超过
`local_zero_max_paths`、回路超过 `local_zero_max_loops` 或符号表达式超限时，再使用
局部系统矩阵。可设置为 `"paths"` 或 `"matrix"` 单独验证两种后端。系统保留路径、
Mason 余因子、局部零点方程、cut 类型、抵消残差和失败原因；无法建立符号候选时返回
`unresolved`，只有显式启用逐根位置控制时才返回 `outside_error_limit`。

完整报告可通过命令行直接生成：

```powershell
python scripts/run_paper_pipeline.py examples/demo_2/circuit.cir `
  --out-dir outputs/demo_2 `
  --f-min 10 --f-max 1e11 `
  --magnitude-error-db 2 --phase-error-deg 5
```

输出包含 `local_zero_localization.md`，其中会列出局部路径、Mason 余因子、
`N_cut(s)=0`、符号零点、cut 类型、抵消残差、角点误差和参数参与度。

### 接入 netLens 视觉网表

netLens 当前输出结构化 HSPICE `.sp`，其中只有实例名、按电气语义排序的引脚和
`nmos4/pmos4/r/c/...` 模型提示。它不能直接作为 SLiCAP 输入：被动元件数值、MOS
工作点小信号参数、输入源、输出检测器以及电源的小信号接地关系都需要补充。

适配层会保留视觉识别得到的拓扑，生成符号参数占位符，并区分两种就绪状态：

- `symbolically_ready`：拓扑、器件类型、输入和输出足以生成合法 `.cir`；
- `numerically_ready`：所有被动元件值和器件小信号参数也已提供，可以进行参考根计算、
  Eq. (13)-(14) 根聚类及误差受控化简。

```python
from sfg_prototype import (
    VisionBridgeConfig,
    bridge_report,
    convert_netlens_sp_to_slicap,
)

bridge = convert_netlens_sp_to_slicap(
    r"outputs/circuit_output.sp",
    VisionBridgeConfig(
        source_node="VIN",
        detector_node="VOUT",
        parameter_values={
            "R1": "10k",
            "gm_M1": "1m",
            "go_M1": "10u",
            "gb_M1": 0,
            "cgs_M1": "1p",
            "cdg_M1": "100f",
            "cgb_M1": 0,
            "cdb_M1": "200f",
            "csb_M1": 0,
        },
    ),
)
cir_path = bridge.write(r"outputs/circuit.cir")
print(bridge_report(bridge))
```

适配器会把 `gnd` 规范化为 SLiCAP 节点 `0`，为 `VIN` 注入独立小信号源，为
`VDD/VSS/VCC/VEE` 插入零增量电源，并将 MOS 编译为 SLiCAP `M` 小信号模型。
PMOS/PNP 参数符号不由图像猜测，必须由工作点分析、工艺模型或用户输入提供。
未知器件、引脚数错误和缺少输入/输出会作为明确错误返回，不会静默进入化简器。

高阶电路可先用描述矩阵接口快速获得数值根。该接口同时返回 `G`、`C`、输入列、
检测行、无穷特征值计数和数值极零相消记录；当前作为独立验证入口，尚未替代
`simplify_netlist()` 中默认的 SLiCAP 符号参考求解：

```python
from sfg_prototype import compute_numeric_descriptor_reference, sample_descriptor_frequency_response

descriptor = compute_numeric_descriptor_reference(r"path\to\circuit.cir")
print(descriptor.poles)
print(descriptor.zeros)
print(descriptor.cancelled_roots)
for sample in sample_descriptor_frequency_response(descriptor, (10.0, 1e3, 1e6)):
    print(sample.frequency_hz, sample.value)
```

也可以让描述矩阵数值根直接驱动 root clustering、Eq. (13)-(14) 分频和图操作误差检查：

```python
config = SimplificationConfig(
    reference_mode="descriptor_mna",
    enable_transfer_term_pruning=False,
)
result = simplify_netlist(r"path\to\large_circuit.cir", config=config)
```

该模式不先构造完整参考符号传函，因此依赖参考 `H(s)` 展开式的全传函逐项删减会自动
跳过；频响误差仍由 `L(G+jwC)^-1B` 直接计算。小图 SPR 定位保留论文式简单路径比较，
较大图使用一次数值 SFG 状态求解比较求和点输入贡献，避免路径组合爆炸。

局部 SPR 符号结果分为两层。`local_spr_relative_frequency_error` 控制从完整局部
Mason 特征式选择主导回路近似时，相对参考闭环根允许的最大频率误差；
`local_spr_term_relative_frequency_error` 则控制在该物理表达式内部继续删除
dominant-product term 时，相对删项前表达式允许的额外设计点误差。若提供
`local_spr_parameter_tolerances`，`local_spr_term_corner_relative_error` 还会限制
所有灵敏度优先参数角点上的删项误差。报告始终同时输出删项前后表达式、保留项数、
参考根误差和最坏角点误差，避免用偶然的数值抵消换取不可解释的短公式。

`enable_transfer_term_pruning` 将同一思想扩展到整个频段传递函数。程序展开
`H_j^*(s)` 的分子、分母，并逐次试删一个乘积项；每个候选直接与原始 SLiCAP
参考传函在该频段完整网格上比较，必须继续满足同一组 Eq. (6)-(7) 幅相或相对误差
边界。可通过 `transfer_term_parameter_tolerances` 提供参数相对容差，例如
`(("gm", 0.1), ("cmu", 0.1))`；程序按传函归一化灵敏度选择参数并检查 min/max
角点。精确 `H_j^*(s)` 始终保留，短式作为 `item.dominant_term_transfer` 并列输出。

面对参数名称未知的新电路，可设置 `transfer_term_default_parameter_tolerance=0.05`
和 `local_spr_default_parameter_tolerance=0.05`，让程序自动对灵敏度最高的参数使用
统一 `±5%` 角点；逐参数 `*_parameter_tolerances` 仍可覆盖默认值。默认值为 `None`，
因为参数容差属于工艺/设计假设，程序不会在用户未声明时擅自假定。

每个频段的短式还会输出参数归一化灵敏度
`S_p^H=(p/H)·∂H/∂p`。`parameter_influences` 按频段内最大
`|S_p^H(jω)|` 排序，并记录代表频点灵敏度、峰值频率、参数出现于分子/分母项和
极点/零点表达式中的次数；`discarded_parameters` 列出误差受控删项后已不再出现在
短式中的参数。两者结合可用于说明该频段由哪些元件参数主导，以及哪些参数在给定
误差预算内可以忽略。

高阶表达式除逐项候选外，还会尝试联合删除分子或分母中相同 `s` 阶次的多个项，
用于识别必须成组处理的弱贡献或数值抵消。每个联合候选仍需通过完整频率网格及参数
角点误差检查，报告会逐步列出被删除的项和删除后的误差。设置
`enable_transfer_joint_order_pruning=False` 可关闭该能力，用于和传统逐项贪心结果对照。
`transfer_term_max_joint_size` 限制一次联合删除的最大项数；候选按频段及参数角点上的
数值贡献从弱到强构造，不会仅因为与主导项具有相同阶次就把主导项一起删除。

默认 `include_subrange_boundaries=True`，会把频段端点加入误差检查，适合严谨验证。论文只说明在“选定的 frequency grid”上控制误差，没有公开具体采样点；复现 Fig. 11 时可显式使用内部采样：

```python
config = SimplificationConfig(
    frequency_range_hz=(10.0, 1.0e11),
    magnitude_error_db=2.0,
    phase_error_deg=5.0,
    include_subrange_boundaries=False,
)
```

该选项不会放宽 `2 dB/5 deg`，只是不强制插入两个 root cluster 的共同分界点。报告会记录这一选择。

候选生成默认兼顾论文完整性和大图可计算性：

```python
config = SimplificationConfig(
    candidate_generation_mode="auto",  # auto / exhaustive / bounded
    exhaustive_candidate_edge_limit=16,
)
```

`exhaustive` 会枚举当前 `G_j` 内全部合法 `RSP` 和极端 order-partition `RR`，不先按数值贡献删除候选；`bounded` 才使用 `rsp_path_relative_threshold` 与 `rr_partition_relative_threshold`。所有真正的接受与拒绝仍由完整频段上的 Eq. (6)-(7) 性能误差决定。

论文第 7 步预化简只处理用户声明的偏置节点合并，并使用通常为用户误差 `1/10000` 的余量：

```python
config = SimplificationConfig(
    bias_node_merges=(("bias_node", "reference_node"),),
    pre_reduction_fraction=1e-4,
)
```

未提供 `bias_node_merges` 时，不会在频段划分前擅自执行 RSP、RR 或 SS。

## 测试

```powershell
python -m pytest -q
```

当前回归覆盖：

- 接地/浮置电压源、独立电流源、VCVS 链和 F/H 控制电流方向；
- 未化简 SFG 与 SLiCAP 参考等价性；
- 论文单管例子的闭环根、Eq. (13)-(14) 边界和 OLR/SPR 分类；
- 描述矩阵广义特征值、Rosenbrock 零点、数值频响和自动 root clustering；
- Eq. (6)-(7) 幅相无穷范数、完整子频段误差边界和 `1/10000` 预化简余量；
- strict complementary graph cut；
- RSP、RR、VPC、SS 的 apply 与原图不变性。
- 有符号两路径 SPR-Z、局部系统矩阵回退、strict-cut SS 和论文 Eq. (24) 端到端恢复。

## 仍需完成

- 以描述矩阵得到的 root cluster 为中心构造局部特征式，为高阶电路完成“不先求完整符号传函”的逐根符号化简路径。
- 将局部零点参数参与度和显式参数角点验证接入大型电路的候选删项，而不仅用于名义设计点定位。
- 为高阶共轭根和近似极零相消增加按 `s` 次数分组的多项联合删减，避免逐项贪心停在局部最优解。
- 将现有单次运行缓存扩展到更昂贵的局部符号求解，并继续降低参数角点组合评估开销。
- 增加更多未知电路、复杂共轭根、多个设计点和参数范围测试。
- 获得论文 OTA 的完整小信号参数后，复现 Fig. 13-17 与 Table I/II；在参数缺失时不把拓扑 draft 当作数值复现结果。
