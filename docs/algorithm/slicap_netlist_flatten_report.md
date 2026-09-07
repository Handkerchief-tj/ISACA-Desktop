# SLiCAP 网表读取、解析与展平分析报告

## 1. 报告目的

本文档用于说明 `SLiCAP` 是如何从 `.cir` 网表文件出发，完成：

1. 词法分析
2. 语法/结构化解析
3. 模型与子电路检查
4. 层次展开与电路展平
5. 生成后续分析可用的小信号电路对象

同时，结合我们前面讨论的 Daems 论文《Circuit Simplification for the Symbolic Analysis of Analog Integrated Circuits》的方法，回顾我们项目第一阶段“读取网表并构造可图化网络模型”的开展方式。

本文重点对应以下源码文件：

- [SLiCAPlex.py](/C:/Anaconda3/envs/slicap_env/Lib/site-packages/SLiCAP/SLiCAPlex.py:1)
- [SLiCAPyacc.py](/C:/Anaconda3/envs/slicap_env/Lib/site-packages/SLiCAP/SLiCAPyacc.py:1)

并补充我们为了项目第一阶段新增的接口文件：

- [SLiCAPnetwork.py](/C:/Anaconda3/envs/slicap_env/Lib/site-packages/SLiCAP/SLiCAPnetwork.py:1)
- [SLiCAPsfg.py](/C:/Anaconda3/envs/slicap_env/Lib/site-packages/SLiCAP/SLiCAPsfg.py:1)

---

## 2. 项目背景与第一阶段目标

### 2.1 项目总目标

我们的总体目标不是只做 `SLiCAP` 式的精确符号传函求解，而是要在其前端能力基础上，结合 Daems 论文中的信号流图化简思路，形成以下流程：

`网表 -> 展平小信号网络 N -> 信号流图 G -> 图化简 -> 根定位/可观测性分析 -> 简化传函与零极点`

其中：

- `SLiCAP` 已经非常擅长 `网表 -> 电路对象 -> MNA -> 精确符号解`
- Daems 论文强调的是 `网络 -> 信号流图 -> 图结构化简 -> 根解释`

因此我们的切入点很自然地落在两者的接口处。

### 2.2 第一阶段范围

第一阶段不直接做完整图化简，而是先完成下面两件事：

1. 复用 `SLiCAP` 的现有前端，读取 `.cir` 网表，完成词法分析、语法解析、库加载、模型检查和层次展开，得到展平后的线性化小信号网络 `N`
2. 在 `N` 的基础上，构造适用于论文方法的信号流图 `G`

所以，理解 `SLiCAPlex.py` 与 `SLiCAPyacc.py` 的逻辑，是整个项目第一阶段的基础。

---

## 3. SLiCAP 网表语法的几个关键约定

从源码和样例可见，`SLiCAP` symbolic 形式网表有几个非常重要的约定：

1. 第一行默认作为电路标题
2. 如果标题含空格，应使用双引号
3. `*` 开头的行为注释，行内 `;` 后面的内容也会被视为注释
4. 元件定义行中，节点后面通常要给出模型名或值
5. 符号表达式要放在 `{}` 中
6. `.source` 指定输入源名
7. `.detector` 指定输出观测量，例如 `V_out`
8. 网表结尾一般有 `.end`

这几点中，最容易踩坑的是“第一行标题”。`SLiCAP` 解析主电路时，会把第一行优先解释为标题，而不是普通元件行，详见 [SLiCAPyacc.py](/C:/Anaconda3/envs/slicap_env/Lib/site-packages/SLiCAP/SLiCAPyacc.py:201)。

因此，如果第一行直接写：

```spice
Vdd Vdd 0 V value=0 dc={Vdd}
```

那么它会被错误地当成标题，导致该元件根本不进入电路对象。这也是我们在测试中看到某些样例少掉 `Vdd` 电源的根本原因。

---

## 4. 词法分析：SLiCAPlex.py 在做什么

`SLiCAPlex.py` 的职责不是理解电路语义，而是把原始文本切成后续解析可处理的“词法记号”。

### 4.1 token 集合

`tokens` 定义在 [SLiCAPlex.py](/C:/Anaconda3/envs/slicap_env/Lib/site-packages/SLiCAP/SLiCAPlex.py:14)，包含：

- `PARDEF`
- `EXPR`
- `SCALE`
- `SCI`
- `FLT`
- `INT`
- `CMD`
- `FNAME`
- `PARAMS`
- `ID`
- `QSTRING`
- `PLUS`
- `LEFTBR`
- `RIGHTBR`
- `COMMENT`
- `NEWLINE`

可以把它们粗分为四类：

1. 结构控制类：`CMD`、`NEWLINE`、`PLUS`
2. 数值/表达式类：`PARDEF`、`EXPR`、`SCALE`、`SCI`、`FLT`、`INT`
3. 标识符类：`ID`、`QSTRING`、`FNAME`
4. 辅助语法类：`LEFTBR`、`RIGHTBR`、`PARAMS`、`COMMENT`

### 4.2 参数定义 `PARDEF`

`t_PARDEF()` 位于 [SLiCAPlex.py](/C:/Anaconda3/envs/slicap_env/Lib/site-packages/SLiCAP/SLiCAPlex.py:22)。

它识别类似下面的写法：

```spice
value={gm}
dc={Vdd}
R=1k
```

这个 token 很关键，因为 `SLiCAP` 后续对模型参数和元件参数的处理，几乎都基于它。

它做了几件事：

1. 去除空白字符
2. 按 `=` 切分成参数名和值
3. 如果值是 `{...}` 表达式，就替换表达式内部的工程量级后缀
4. 如果值是单纯数值，也会把 `1k` 这种形式改写为科学计数
5. 最终调用 `sympy.sympify(..., rational=True)` 转为 `sympy` 对象

这意味着：在进入语法分析前，很多看似“字符串”的参数值，已经被转换成了 `sympy.Symbol`、`sympy.Expr` 或有理数。

### 4.3 表达式 `EXPR`

`t_EXPR()` 位于 [SLiCAPlex.py](/C:/Anaconda3/envs/slicap_env/Lib/site-packages/SLiCAP/SLiCAPlex.py:115)。

它专门处理 `{...}` 包裹的表达式，比如：

```spice
value={1/(s*C)}
value={gm}
value={2*pi*1M}
```

其流程和 `PARDEF` 中的表达式处理类似：

1. 在 `{}` 内部替换量级后缀
2. 用 `sympy.sympify()` 转成符号对象

因此，`SLiCAP` 的 symbolic 网表之所以能“天然进入符号分析”，是因为在词法阶段就已经把参数文本接入了 `sympy`。

### 4.4 命令、注释和续行

几个很重要的规则分别由这些函数处理：

- `.source`、`.detector`、`.model`、`.subckt` 等命令由 `t_CMD()` 识别，[SLiCAPlex.py](/C:/Anaconda3/envs/slicap_env/Lib/site-packages/SLiCAP/SLiCAPlex.py:72)
- `*` 行注释与 `;` 行内注释由 `t_COMMENT()` 忽略，[SLiCAPlex.py](/C:/Anaconda3/envs/slicap_env/Lib/site-packages/SLiCAP/SLiCAPlex.py:80)
- 行首 `+` 续行由 `t_PLUS()` 配合 `_tokenize()` 实现，[SLiCAPlex.py](/C:/Anaconda3/envs/slicap_env/Lib/site-packages/SLiCAP/SLiCAPlex.py:204)、[SLiCAPlex.py](/C:/Anaconda3/envs/slicap_env/Lib/site-packages/SLiCAP/SLiCAPlex.py:239)

尤其 `_tokenize()` 中这一段逻辑值得注意：

1. 正常 token 累加到 `lastLine`
2. 遇到 `NEWLINE` 时，把 `lastLine` 作为一条逻辑行加入 `lines`
3. 遇到 `PLUS` 时，把新一行并回上一条逻辑行

因此，对后端来说，“一条网表语句”不一定等于“物理上一行文件”，而是 `_tokenize()` 组合后的逻辑行。

### 4.5 错误定位机制

`_printError()`、`_find_column()`、`_get_input_line()` 分别位于：

- [SLiCAPlex.py](/C:/Anaconda3/envs/slicap_env/Lib/site-packages/SLiCAP/SLiCAPlex.py:272)
- [SLiCAPlex.py](/C:/Anaconda3/envs/slicap_env/Lib/site-packages/SLiCAP/SLiCAPlex.py:297)
- [SLiCAPlex.py](/C:/Anaconda3/envs/slicap_env/Lib/site-packages/SLiCAP/SLiCAPlex.py:310)

它们的作用是：

1. 找到出错 token 所在列
2. 取出原始输入行
3. 打印带 `|` 指示符的位置

这使得 `SLiCAP` 在网表语法错误时能够给出比较友好的报错信息。

---

## 5. 多遍解析：SLiCAPyacc.py 的整体框架

`SLiCAPyacc.py` 并没有使用传统 yacc 风格的大型文法规则，而是采用了“词法结果 + 多遍程序化解析”的设计。

文件开头已经明确写出了四个 pass，见 [SLiCAPyacc.py](/C:/Anaconda3/envs/slicap_env/Lib/site-packages/SLiCAP/SLiCAPyacc.py:5)：

1. Pass 1：把网表转成嵌套电路对象
2. Pass 2：检查引用、模型和库，补全对象关系
3. Pass 3：展开模型与子电路，得到 flat 电路
4. Pass 4：整理节点、变量、参数等后续分析所需数据

这四步正是我们理解“SLiCAP 如何把网表变成可分析小信号模型”的主线。

---

## 6. Pass 1：从 token 行到嵌套电路对象

### 6.1 入口 `_parseNetlist()`

Pass 1 的核心入口是 [SLiCAPyacc.py](/C:/Anaconda3/envs/slicap_env/Lib/site-packages/SLiCAP/SLiCAPyacc.py:175) 中的 `_parseNetlist(netlist, name, cirType)`。

它做的第一件事是：

1. 把当前电路名压入 `_CIRCUITNAMES`
2. 创建一个新的 `circuit` 对象
3. 调用 `_tokenize(netlist)` 获取逻辑行序列

### 6.2 主电路标题处理

如果当前是主电路 `main`，则它会检查第一条逻辑行是否属于 `_TITLE` 类型，见 [SLiCAPyacc.py](/C:/Anaconda3/envs/slicap_env/Lib/site-packages/SLiCAP/SLiCAPyacc.py:201)。

这里 `_TITLE = ['ID', 'QSTRING', 'FNAME']`，定义在 [SLiCAPyacc.py](/C:/Anaconda3/envs/slicap_env/Lib/site-packages/SLiCAP/SLiCAPyacc.py:52)。

这就是为什么第一行默认被视为标题。

### 6.3 行分类：元件行还是命令行

`_parseNetlist()` 对每条逻辑行做简单分派：

- 如果首 token 是 `ID`，认为是元件行
- 如果首 token 是 `CMD`，认为是命令行

其中元件行又分两类：

- 普通元件：`_parseElement()`
- 子电路实例 `X...`：`_parseSubcircuitElement()`

命令行中，特殊处理的有：

- `.SUBCKT`
- `.SOURCE`
- `.DETECTOR`
- `.LGREF`
- `.ENDS`
- `.END`

其它命令再交给 `_parseCommand()`。

### 6.4 普通元件 `_parseElement()`

核心函数是 [SLiCAPyacc.py](/C:/Anaconda3/envs/slicap_env/Lib/site-packages/SLiCAP/SLiCAPyacc.py:395)。

它的解析思路非常直接：

1. 从元件名首字母确定器件类型，例如 `R1` 对应 `R`
2. 查 `_DEVICES[deviceType]` 获取该器件需要几个节点、几个引用、是否必须有 value/model 字段
3. 顺序取出：
   - `nodes`
   - `refs`
   - `model` 或 `value`
   - 后续参数定义

这里 `_DEVICES` 和 `_MODELS` 在 `SLiCAPprotos.py` 中定义，它们等价于一张“器件模板表”和“模型模板表”。

普通元件解析完成后会得到一个 `element` 对象，暂时挂到当前 `circuit.elements` 中。

### 6.5 子电路实例 `_parseSubcircuitElement()`

子电路实例解析在 [SLiCAPyacc.py](/C:/Anaconda3/envs/slicap_env/Lib/site-packages/SLiCAP/SLiCAPyacc.py:481)。

其基本格式为：

```spice
X1 n1 n2 subcktName par1={...} par2={...}
```

解析逻辑是：

1. 找到“模型位置”，也就是子电路原型名
2. 前面的 token 作为节点
3. 后面的 `PARDEF` 作为实例参数

注意，这时并不会立刻展开，只是把它存成一个 `type='X'` 的元素对象。

### 6.6 命令行 `_parseCommand()`

命令解析在 [SLiCAPyacc.py](/C:/Anaconda3/envs/slicap_env/Lib/site-packages/SLiCAP/SLiCAPyacc.py:528)。

其中最关键的几类命令是：

- `.PARAM`
  写入 `parDefs`
- `.MODEL`
  生成 `modelDef`
- `.LIB` / `.INC...`
  交给 `_parseLibrary()`

`.source` 和 `.detector` 虽然没有走 `_parseCommand()`，但也在 `_parseNetlist()` 主循环中被单独识别并存入 `circuit.source` 与 `circuit.detector`。

### 6.7 库文件 `_parseLibrary()`

库加载函数位于 [SLiCAPyacc.py](/C:/Anaconda3/envs/slicap_env/Lib/site-packages/SLiCAP/SLiCAPyacc.py:608)。

它会按下面顺序找库文件：

1. 绝对路径或相对当前项目路径
2. `ini.cir_path`
3. `ini.user_lib_path`

找到后调用 `_compileUSERLibrary()` 将其中的模型、参数、子电路编译进全局库对象中。

从项目角度看，这一步很重要，因为它让一个主网表在 Pass 1 结束时，就已经具备了“后面可以查到外部模型原型”的能力。

---

## 7. Pass 2：检查引用并把模型/子电路原型接上

Pass 2 的入口是 [SLiCAPyacc.py](/C:/Anaconda3/envs/slicap_env/Lib/site-packages/SLiCAP/SLiCAPyacc.py:679) 的 `_checkReferences(circuitObject)`。

其逻辑是：

1. `_checkElementReferences()`
2. `_checkModelDefs()`
3. `_checkElementModel()`
4. 对子电路递归做同样检查

### 7.1 引用检查

`_checkElementReferences()` 位于 [SLiCAPyacc.py](/C:/Anaconda3/envs/slicap_env/Lib/site-packages/SLiCAP/SLiCAPyacc.py:705)。

它主要检查受控源等“引用其它元件电流/支路”的场景，确保 `refs` 中出现的名字在当前电路内真实存在。

### 7.2 `.model` 定义检查

`_checkModelDefs()` 位于 [SLiCAPyacc.py](/C:/Anaconda3/envs/slicap_env/Lib/site-packages/SLiCAP/SLiCAPyacc.py:720)。

这里会验证 `.model` 行给出的参数名，是否属于对应基本模型允许的参数集合。

### 7.3 非子电路元件模型解析

`_checkElementModelParams()` 位于 [SLiCAPyacc.py](/C:/Anaconda3/envs/slicap_env/Lib/site-packages/SLiCAP/SLiCAPyacc.py:776)。

它解决了几个核心问题：

1. 如果没有显式写模型名，则使用该器件的默认基本模型
2. 如果写的是用户模型名，则去本地定义、用户库或系统库中查找
3. 把实例上的参数与模型模板进行对齐
4. 对没给出的参数填默认值
5. 如果这个模型不是“直接 stamp 的基本模型”，而是需要展开的复合模型，则把 `el.model` 替换成相应的 `circuit` 原型

这一步是“小信号模型化”的关键来源之一。

例如一个 MOS 元件 `M1` 在网表里可能是一个四端晶体管模型，但经过这里的处理后，如果其基本模型在 `SLiCAP` 中被定义为一个可展开子电路，那么它后续会在 Pass 3 中被展开成若干 `g`、`C`、`r` 等小信号元件。

### 7.4 子电路实例模型解析

`_checkSubCircuitElementModelParams()` 位于 [SLiCAPyacc.py](/C:/Anaconda3/envs/slicap_env/Lib/site-packages/SLiCAP/SLiCAPyacc.py:848)。

它会把一个 `X...` 元件的 `model` 名字，替换成真正的 `circuit` 原型对象，并检查：

1. 调用时给出的参数名是否合法
2. 子电路调用参数中是否非法使用了拉普拉斯变量

经过 Pass 2 后，很多原本只是字符串名字的 `model` 字段，已经被替换成了可展开的原型对象。

---

## 8. Pass 3：层次展开与电路展平

Pass 3 是我们最关心的部分，因为它直接决定了我们能否拿到“展平的小信号电路模型 `N`”。

### 8.1 展平入口 `_expandCircuit()`

函数位于 [SLiCAPyacc.py](/C:/Anaconda3/envs/slicap_env/Lib/site-packages/SLiCAP/SLiCAPyacc.py:896)。

它的逻辑很简单：

1. 遍历当前电路里的所有元素
2. 只要发现 `el.model` 已经是一个 `circuit` 对象
3. 就调用 `_doExpand()` 把它展开

换言之，Pass 2 负责“把该展开的模型标成 `circuit` 原型”，Pass 3 负责“真正把它们展开”。

### 8.2 展开核心 `_doExpand()`

函数位于 [SLiCAPyacc.py](/C:/Anaconda3/envs/slicap_env/Lib/site-packages/SLiCAP/SLiCAPyacc.py:916)。

其核心步骤如下：

1. 取出父实例的：
   - 实例名 `parentRefDes`
   - 外部连接节点 `parentNodes`
   - 实例参数 `parentParams`
2. 取出原型子电路的：
   - 端口节点 `prototypeNodes`
   - 默认参数 `prototypeParams`
   - 内部参数定义 `childParDefs`
3. 用 `_updateParDefs()` 先把父/子参数环境合并
4. 遍历原型子电路中的每一个内部元件，做深拷贝
5. 对每个新元件依次完成：
   - 改引用名
   - 改节点名
   - 改参数表达式
   - 写回父电路
6. 如果新元件本身还是子电路，则递归展开
7. 最后删除原来的父实例元素

这一步执行结束后，原来的层次边界就被真正打散了。

### 8.3 节点映射 `_updateNodes()`

函数位于 [SLiCAPyacc.py](/C:/Anaconda3/envs/slicap_env/Lib/site-packages/SLiCAP/SLiCAPyacc.py:947)。

节点更新规则非常关键：

1. 如果某个内部节点属于原型端口列表，则替换成父实例对应位置的实际连接节点
2. 否则，这说明它是原型内部节点，需要重命名为：

```text
<原内部节点名>_<父实例refdes>
```

这样做有两个作用：

1. 保持端口连接正确
2. 防止不同子电路实例的内部节点重名

### 8.4 参数替换 `_updateElementParams()`

函数位于 [SLiCAPyacc.py](/C:/Anaconda3/envs/slicap_env/Lib/site-packages/SLiCAP/SLiCAPyacc.py:982)。

对一个新展开出来的元件，它参数表达式中的符号可能来自很多地方：

1. 父实例传入参数
2. 原型子电路默认参数
3. 用户库参数
4. 系统库参数
5. 原型内部局部参数

该函数通过扫描每个参数表达式中的 `sympy.Symbol` 原子，建立替换字典 `substDict`，然后用 `fullSubs()` 做完整替换。

如果某个参数既不属于父参数、也不属于原型参数、也不属于全局库参数，那么它会被重命名为：

```text
<参数名>_<父实例refdes>
```

这与内部节点改名策略是一致的，目的都是避免实例间冲突。

### 8.5 参数定义合并 `_updateParDefs()`

函数位于 [SLiCAPyacc.py](/C:/Anaconda3/envs/slicap_env/Lib/site-packages/SLiCAP/SLiCAPyacc.py:1064)。

它处理的是“子电路内部的参数定义”如何并入父电路。

本质上，它对参数定义也做了一次“作用域展开”：

1. 如果参数来自父实例或原型参数，就用已有值替换
2. 如果来自用户/系统全局库，就补进父级参数定义表
3. 否则，就附加 `_父实例名` 后缀形成新的局部化参数名

因此，Pass 3 不只是元件层面的展开，还伴随了参数命名空间的展开。

---

## 9. Pass 4：展平后电路对象的整理

Pass 4 的入口是 [SLiCAPyacc.py](/C:/Anaconda3/envs/slicap_env/Lib/site-packages/SLiCAP/SLiCAPyacc.py:1167) 中的 `_updateCirData()`。

经过 Pass 3 后，电路已经基本是 flat 的了，但还需要把它整理成后续分析能够直接使用的形式。

### 9.1 建立节点、变量和源列表

`_updateCirData()` 会遍历所有已展开元件，收集：

- `nodes`
- `indepVars`
- `controlled`
- `references`

其中：

- 独立源来自 `_INDEPSCRCS = ['I', 'V']`
- 受控源来自 `_CONTROLLED = ['E', 'F', 'G', 'H']`

### 9.2 建立 dependent variables

它会为某些元件追加支路因变量名，例如电压源、电感等在 MNA 中需要引入附加电流变量。

随后又会统一生成节点电压变量：

```text
V_<nodeName>
```

也就是说，Pass 4 的结果已经非常接近 MNA 所需的数据布局。

### 9.3 整理参数

它会：

1. 把 `parDefs` 的字符串 key 统一转成 `sympy.Symbol`
2. 从元件参数表达式和参数定义表达式中，提取所有出现过的符号
3. 尝试自动补入系统全局参数定义
4. 最终把“仍未定义”的符号放进 `circuit.params`

因此，展平电路对象不仅包含元件和节点，还包含了一个比较完整的参数环境。

### 9.4 基本合法性检查

Pass 4 还会检查：

1. 是否存在地节点 `0`
2. `.source` 中指定的源是否真的是独立源
3. `.lgRef` 中指定的引用是否真的是受控源

经过这一阶段之后，`SLiCAP` 才认为这个电路对象可以交给后续指令系统和矩阵构造模块。

---

## 10. `_checkCircuit()`：四个 pass 串起来的总入口

总入口是 [SLiCAPyacc.py](/C:/Anaconda3/envs/slicap_env/Lib/site-packages/SLiCAP/SLiCAPyacc.py:1280) 的 `_checkCircuit(fileName)`。

它的主流程非常清楚：

1. `_resetParser()`
2. 读取网表文本
3. Pass 1：`_parseNetlist(...)`
4. Pass 2：`_checkReferences(...)`
5. Pass 3：`_expandCircuit(...)`
6. Pass 4：`_updateCirData(...)`
7. 若无错误，再创建 HTML 页面

从我们项目的视角看，真正需要的核心输出就是：

```python
_CIRCUITS['main']
```

也就是“展平并更新完成的主电路对象”。

---

## 11. SLiCAP 如何把电路展平成“小信号模型”

这里需要强调一个很重要的认识：

`SLiCAP` 的“展平”并不只是把层次子电路拍扁，它还经常把复合器件展开成小信号等效元件。

例如在我们的真实样例里：

- 网表里原本是 `M1 ... M`
- 展平后可能出现：
  - `Gm_M1 g`
  - `Go_M1 g`
  - `Cdg_M1 C`
  - `Cgs_M1 C`
  - `Rb_Q r`

这说明：

1. 原始晶体管模型在 Pass 2 中被识别为一个需要展开的模型原型
2. 在 Pass 3 中，它被替换成若干基础小信号元件

因此，我们第一阶段所说的“得到展平的线性化小信号电路模型 `N`”，在工程上对应的并不是“原始大器件列表”，而是：

**一个已经尽可能被展开成基础元件级别的、无层次或低层次残留的线性小信号网络对象**

这正是 `SLiCAP` 对我们最有价值的地方。

---

## 12. 第一阶段：我们是如何开展的

### 12.1 为什么复用 SLiCAP 前端

如果从零开始写一个支持 symbolic 参数、子电路、模型库、层次展开、参数传递的小信号网表前端，工作量非常大，而且容易出错。

相比之下，`SLiCAP` 已经解决了：

1. symbolic 表达式的解析
2. 工程量级后缀处理
3. `.source/.detector/.model/.param/.subckt/.include/.lib` 的识别
4. 模型和子电路原型查找
5. 层次展开
6. 参数作用域展开
7. 小信号基础元件化

所以我们第一阶段的正确策略是：

**不重写网表前端，而是把 `SLiCAP` 当作一个可靠的“网表解析与展平引擎”。**

### 12.2 我们新增的 `LinearNetwork`

为了不让后续图算法直接耦合 `SLiCAP` 内部对象，我们在 [SLiCAPnetwork.py](/C:/Anaconda3/envs/slicap_env/Lib/site-packages/SLiCAP/SLiCAPnetwork.py:47) 中定义了 `LinearNetwork` 和 `LinearElement`。

它们的作用是：

1. 接收 `SLiCAP` 展平后的 `circuit`
2. 复制出更稳定、面向项目的数据中间层
3. 为后续 `N -> G` 转换和图化简阶段提供统一接口

对应入口是 [flatten_netlist()]( /C:/Anaconda3/envs/slicap_env/Lib/site-packages/SLiCAP/SLiCAPnetwork.py:124 )。

### 12.3 纯解析 API 的处理

原始 `_checkCircuit()` 有一个副作用：成功后会尝试生成 HTML 报告页面。

这对标准 `SLiCAP` 项目流程是合理的，但对我们这种“只想取展平网络对象”的程序化调用不够友好。

因此我们在 `flatten_netlist()` 中临时屏蔽了这一步，使其更接近一个“纯解析/展平 API”。

### 12.4 我们新增的 `SignalFlowGraph`

为了接上论文方法，我们在 [SLiCAPsfg.py](/C:/Anaconda3/envs/slicap_env/Lib/site-packages/SLiCAP/SLiCAPsfg.py:95) 中定义了：

- `GraphVertex`
- `GraphContribution`
- `MetaEdge`
- `SignalFlowGraph`

并提供了 [build_signal_flow_graph()]( /C:/Anaconda3/envs/slicap_env/Lib/site-packages/SLiCAP/SLiCAPsfg.py:199 ) 和 [netlist_to_signal_flow_graph()]( /C:/Anaconda3/envs/slicap_env/Lib/site-packages/SLiCAP/SLiCAPsfg.py:225 )。

### 12.5 `N -> G` 的当前策略

我们目前采用的是“从展平元件表直接构图”，而不是从 MNA 逆推信号流图。

原因是：

1. 更接近论文中的“元件印章 -> 图印章”思路
2. 更能保留拓扑语义
3. 更方便后续做冗余边删除、meta-edge 合并、open-loop root 检测

当前第一版已支持：

- `R`
- `r`
- `C`
- `L`
- `g`
- 接地独立电压源 `V`
- 独立电流源 `I`

而更复杂的情况会暂时记入 `deferred_elements`，等待第二阶段继续补齐。

### 12.6 一个很重要的实际发现

通过对真实网表示例的测试，我们发现很多复杂器件在 `SLiCAP` 展平后，已经不再以原始 `M`、`Q` 的形式存在，而是变成了：

- `C`
- `R`
- `r`
- `g`
- `V`

这意味着我们的图转换模块不需要一开始就支持所有高层器件，只要优先覆盖“展平后高频出现的基础元件模型”，就能较快打通第一阶段。

---

## 13. 与 Daems 论文方法的衔接

Daems 论文中的核心思想是：

1. 从线性化网络模型 `N` 出发
2. 构造双顶点信号流图 `G`
3. 进行冗余消除、图预化简、根聚类、局部子图分析
4. 定位 open-loop roots 与 summing-point roots
5. 检查 root observability
6. 写出具有解释性的简化极点、零点与传函表达式

而 `SLiCAP` 给我们的最大帮助是：

它已经把“从网表获得正确的小信号网络模型 `N`”这一步做得非常扎实。

因此，本项目第一阶段实际上是在完成论文方法中的“Step 0”到“Step 1”之间的工程搭桥：

`SLiCAP netlist frontend -> flattened small-signal network N -> project-specific signal-flow graph G`

只有 `N` 足够可靠，后续 `G` 的拓扑分析和图化简才有意义。

---

## 14. 当前结论

基于对 `SLiCAPlex.py` 与 `SLiCAPyacc.py` 的阅读，可以得出下面几点结论：

1. `SLiCAP` 的网表处理不是单次解析，而是“四遍式解析与重写”
2. `SLiCAPlex.py` 在词法阶段就把大量数值/表达式转成了 `sympy` 对象
3. `SLiCAPyacc.py` 的 Pass 2 和 Pass 3 不只是“查表”，而是在逐步把网表提升为一个可展开、可替换、可参数化的小信号模型
4. 最终得到的不是简单的原始器件列表，而是一个适合 MNA 和后续符号分析的展平网络对象
5. 对我们的项目来说，`SLiCAP` 最有价值的能力就是“稳定地给出展平线性化小信号网络 `N`”
6. 我们第一阶段已经在这个基础上完成了独立中间层 `LinearNetwork` 的建立，并开始构造论文所需的信号流图 `G`

---

## 15. 后续工作建议

在第一阶段结束后，后续工作建议按以下顺序推进：

1. 继续补全 `N -> G` 的元件 stamp 支持，覆盖更多展平后常见模型
2. 实现论文中的冗余边删除与一致性修正
3. 对 `meta-edge` 做按 `s` 阶次的分区分析
4. 实现 root clustering 与频段划分
5. 实现局部子图分析、summing-point root 检测与 observability 检查
6. 最终输出简化传递函数与零极点结果

从研究实施路径上看，这是一条相对稳妥、层次清晰且与现有 `SLiCAP` 能力高度兼容的路线。
