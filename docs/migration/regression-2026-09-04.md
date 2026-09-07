# 60 网表结构回归

## 范围

- 单管：14 个 `.cir`。
- 双管：2 个 `.cir`。
- 多管：44 个 `.cir`。
- 环境：Python 3.12.14、SLiCAP 5.2.1。
- 模式：调用 `makeCircuit()` 并取得展平电路，不要求数值参数完整。

## 结果

| 状态 | 数量 | 解释 |
|---|---:|---|
| `structure_passed` | 44 | SLiCAP 5.2.1 成功解析并展平 |
| `failed` | 16 | 测试库旧草稿的网表语法/模型错误 |

开启数值筛选后，44 个有效文件进一步分为：20 个参数完整案例成功执行
`doLaplace/doPZ`，24 个符号网表仅执行结构展平并准确记录缺失参数。没有用默认值
或常数 1 补齐符号案例。

## 4.0.8 与 5.2.1 数值对照

20 个参数完整案例分别在旧 `slicap_env` 和新 `slicap5_env` 的独立子进程中运行。
结果为 20/20 通过：

- 20 个传递函数经 SymPy 化简后均符号等价。
- 六个复频率采样点上的最大相对差为 `5.387380942283987e-16`。
- 匹配后的极点最大相对差为 0。
- 匹配后的零点最大相对差为 0。

逐文件对照位于 `runs/regression/slicap408-vs-521.json`。

第一轮曾有 20 个 UTF-8 中文文件因 Windows 默认编码失败。适配器改为只将 SLiCAP
临时工作副本转码为系统首选编码后，这类失败降为 0；UTF-8 原件和输出制品不变。

## 16 个旧草稿失败的原因

这些文件位于多管 case2、3、6、7、8、9、10、11，每个 case 有“无数值”和
“含寄生电容”两个旧版本。共同问题是把 PMOS 写成：

```text
M2 ... P gm={gm2} ...
```

其中 `P` 没有 `.model P ...` 定义，也不是 SLiCAP 内置 MOS 小信号模型，因而官方
解析器报告 `missing definition of model: P`。case2 旧草稿另有：

```text
Is VDD drain DC {Is}
```

这不是 SLiCAP 独立电流源语法；修正版使用 `Isrc VDD drain I dc={Is} value=0`。

同目录的 `审查修正版符号` 和 `审查修正版数值` 均成功通过。故这 16 个失败应标为
obsolete/invalid fixtures，不应归因于 SLiCAP 5.2.1 或适配层不支持对应电路拓扑。

机器可读逐文件记录位于运行目录
`runs/regression/slicap521-structure.json`，该目录不提交 Git。
