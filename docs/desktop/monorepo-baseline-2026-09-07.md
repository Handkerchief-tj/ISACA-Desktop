# 单仓库基线验收记录

日期：2026-09-07

## 目标

将原先分别位于桌面集成仓库和算法仓库的源码整理成一个可克隆、可安装、可测试的
`ISACA-Desktop` 仓库。此次迁移只改变源码布局、安装方式和文档，不改变 SFG 算法行为。

## 来源

- 桌面与统一分析核心：`Analog-Circuit-Analyzer` 提交 `425732e`。
- SFG 算法：`Intelligent-Symbolic-Analog-Circuit-Analyzer` 提交 `b7d5759`，
  原独立包版本 `0.2.3`。

源码统一放入：

```text
src/isaca_desktop
src/isaca_api
src/sfg_prototype
```

仓库只有一个 `.git` 和一个 `pyproject.toml`。未引入 Git submodule、嵌套仓库或远程
算法安装依赖。

## 自动化测试

执行命令：

```powershell
C:\Anaconda3\envs\slicap5_env\python.exe -m pytest tests -q -p no:cacheprovider
```

结果：

```text
98 passed, 1 skipped, 1 warning in 215.08s
```

唯一跳过项是按项目决定暂缓的视觉验收。唯一 warning 来自 Starlette TestClient 的
上游弃用提示，不影响功能结果。

导入路径检查确认 `isaca_desktop`、`isaca_api` 和 `sfg_prototype` 均来自本仓库
`src/`，没有引用旧源码目录。

## 单一制品检查

根目录 `pyproject.toml` 成功构建：

```text
isaca_desktop-0.1.0-py3-none-any.whl
size: 1,149,561 bytes
sha256: efeb86f53ecd572a857e61bcda76c158cc1ae33fd15553a3db3028206837c839
```

wheel 同时包含三组运行包及离线 KaTeX 资源，不包含测试、旧 React Web Schematic、
旧项目 `SLiCAP/` 目录、模型权重或运行缓存。

`setup.ps1 -SkipTests` 已在 Python 3.12 环境实际完成 editable install，安装后的
`python -m isaca_desktop --help` 正常显示项目、文件和 worker 参数。

## 端到端验证

### RC low-pass

- 用时：2.75 s。
- 极点：`-1000 rad/s`。
- 零点：无。
- 诊断：无。

### demo_2_numeric

- 用时：147.531 s。
- 数值根：3 个极点、2 个零点。
- root cluster：4 个。
- 接受的图操作：21 个。
- 5 个目标根均得到符号解释。
- 自动断言论文 Eq. (22) 与 Eq. (24) 的符号等价性。

| Cluster | Frequency range / Hz | Roots | Max magnitude error / dB | Max phase error / deg |
|---:|---:|---:|---:|---:|
| 1 | 10 - 3.399850e3 | 1 | 3.362558e-4 | 7.882563e-3 |
| 2 | 3.399850e3 - 4.685967e5 | 1 | 5.884220e-2 | 4.194397 |
| 3 | 4.685967e5 - 9.823256e7 | 1 | 8.942093e-2 | 4.266253 |
| 4 | 9.823256e7 - 1.0e11 | 2 | 8.964611e-2 | 3.697497 |

全部频段满足配置的 `±2 dB`、`±5 deg` 传递性能限制。唯一诊断为
`graphviz_unavailable`，表示尚未配置发布版私有 Graphviz，仅影响 SFG SVG 渲染。

## 当前边界

- 这是可运行的桌面源码开发版，不是 standalone EXE。
- 视觉模块接口保留，但视觉性能与模型分发不在当前验收范围。
- 下一阶段先扩大官方画布回归，再构建和验证 standalone 安装包。
