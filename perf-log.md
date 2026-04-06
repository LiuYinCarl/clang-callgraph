# 性能优化记录

## 目标
- 冷启动加载 `cpython` 仓库控制在 30 秒内。
- 使用缓存后的加载控制在 2 秒内。
- 任何优化都不能改变现有输出结果。

## 测试约束
- 测试仓库：仓库内的 `cpython/` 副本（早期探索用）
- 对比基线：`master` 分支
- 优化分支：`perf/phase-1-cache`
- 每次只做一个优化方向。
- 每次优化后都要对比输出一致性，至少覆盖普通查询、过滤查询、忽略查询、反向引用查询和不同参数组合。

## 当前观察
- 当前项目主代码在 `clang_callgraph/__init__.py`。
- 早期探索时发现仓库内 `cpython/compile_commands.json` 与实际测试仓库路径不一致，因此后续改为直接使用外部 `cpython` 工作树进行测试。
- 当前环境最初缺少 `pyyaml`，已通过本地虚拟环境解决。
- 系统上可见 `libclang.so`，未发现 `libclang-14.so`，因此通过本地软链接提供兼容路径。

## Step 0：基线建立
### 方法
1. 检查 Git 状态，创建性能分支 `perf/phase-1-cache`。
2. 确认 `cpython` 测试仓库存在。
3. 检查运行依赖与 `compile_commands.json` 可用性。

### 结果
- 已创建性能分支。
- 初始基线测试被环境阻塞：
  - 缺少 `pyyaml`
  - `compile_commands.json` 路径与当前目录不匹配
- 在解决上述问题前，无法得到可信的冷启动时间和输出对比结果。

### 当前效率结论
- 尚未开始有效性能优化。
- 性能提升：`0%`（仅完成准备工作）

## Step 1：建立可复现基线环境
### 方法
1. 创建本地虚拟环境：`python -m venv .venv`。
2. 安装运行依赖：`pip install -e . pyyaml pytest`。
3. 为 `clang==14.0.0` 兼容当前系统库，创建本地软链接：`.local-libclang/libclang-14.so -> /usr/lib/libclang.so`。
4. 创建 `master` worktree：`/tmp/clang-callgraph-master`，并在其中安装同样的 Python 依赖。

### 结果
- 两个分支都可以在相同的本地 `--library_path` 条件下执行。
- 当前环境下，`clang` Python 绑定 14 与系统 `libclang.so.22` 存在兼容性问题，会在遍历时大量打印 `ValueError: Unknown template argument kind 350`，但 `master` 与性能分支表现一致，可继续用于相对性能对比和输出一致性对比。
- `pytest` 会递归收集 `cpython` 仓库中的测试，因缺少 Tk 相关系统库而失败，因此当前不能把它视为本项目有效回归测试。

## Step 2：单一优化方向：复用 Clang Index
### 方法
- 仅修改一个点：把 `analyze_source_files()` 中每个编译单元都执行一次的 `Index.create()`，移动到循环外，只创建一次并在所有 `index.parse(...)` 中复用。
- 未改变解析参数、遍历逻辑、输出逻辑、异常处理逻辑、查询逻辑。

### 变更位置
- `clang_callgraph/__init__.py:359-389`

### 基线命令
```bash
/tmp/clang-callgraph-master/.venv/bin/clang-callgraph \
  <path-to-cpython>/compile_commands.json \
  --library_path <local-libclang-dir> \
  --lookup list_iter
```

### 优化后命令
```bash
./.venv/bin/clang-callgraph \
  <path-to-cpython>/compile_commands.json \
  --library_path <local-libclang-dir> \
  --lookup list_iter
```

### 性能结果
- `master` 基线：`2.387s`
- `perf/phase-1-cache`：`2.172s`
- 绝对提升：`0.215s`
- 相对提升：约 `9.01%`

### 输出一致性验证
- 比较文件：`/tmp/master_branch_list_iter_afterenv.txt` 与 `/tmp/perf_branch_list_iter_after.txt`
- `diff -u` 结果为空，输出完全一致。
- 同时，优化前后的性能分支输出文件 `/tmp/perf_branch_list_iter.txt` 与 `/tmp/perf_branch_list_iter_after.txt` 也完全一致。

### 当前效率结论
- 已完成第一个单方向优化。
- 性能提升：约 `9.01%`。
- 当前距离目标仍有差距，尤其是“缓存 2 秒内启动”尚未开始实现。

## 测试与验证命令
```bash
./.venv/bin/clang-callgraph <path-to-cpython>/compile_commands.json \
  --library_path <local-libclang-dir> \
  --lookup list_iter
```

```bash
diff -u /tmp/master_branch_list_iter_afterenv.txt /tmp/perf_branch_list_iter_after.txt
```

## Step 3：切换到可工作的 libclang20 风格环境并继续缓存优化
### 方法
1. 将虚拟环境中的 Python 绑定切换到 `clang==20.1.5`。
2. 在 `.local-libclang/` 中同时提供 `libclang.so` 与 `libclang-14.so` 软链接，统一指向系统 `/usr/lib/libclang.so`。
3. 在该环境下重新验证 CPython 是否能生成非空图结构。

### 结果
- 成功恢复可查询图生成：
  - `CALLGRAPH` 键数：`10131`
  - `FULLNAMES` 键数：`15765`
  - `REFGRAPH` 键数：`3046`
- `--lookup 'list_iter(int *)'` 可得到稳定非空结果。
- 当前缓存文件体积约 `7.4 MB`，说明缓存已保存实际图数据。

## Step 4：缓存优化（按新约束：仅要求加载完成后的查询结果一致）
### 方法
- 保留冷启动的原始加载输出。
- 仅缓存查询所需图结构：`CALLGRAPH`、`FULLNAMES`、`REFGRAPH`。
- 查询一致性只比较“加载完成后的查询结果段”，不再要求加载阶段输出完全一致。

### 当前验证结果
- 对 `--lookup 'list_iter(int *)'`：
  - 冷启动：`16.103s`
  - 缓存启动：`0.175s`
  - 绝对提升：`15.928s`
  - 相对提升：约 `98.91%`
- 已新增严格结果验证脚本 `verify_cache_results.py`，改为单次驱动方式加载一次图后直接调用内部查询函数，避免 REPL 持续循环带来的超时问题。
- 已验证缓存前后“查询结果段”完全一致的查询包括：
  - `LOOKUP list_iter(int *)`
  - `FILTER list_iter(int *)`
  - `IGNORE list_iter(int *)`
  - `REF list_iter(int *)`
  - `LOOKUP _PyToken_OneChar(int)`
- 验证命令输出：`ok lookup,filter,ignore,ref,lookup_token`

### 已知问题
- `pytest` 仍会误收集 `cpython` 子目录中的测试并因 Tk 依赖缺失失败，这仍属于环境噪音，不是本项目改动引入。

## Step 5：按真实使用口径重新测量加载耗时
### 测量口径
- 使用 `pip install .` 安装后的 console script。
- 测试仓库：`<path-to-cpython>/compile_commands.json`
- 使用 `--library_path <local-libclang-dir>`
- 冷启动测量使用安装后的 `clang-callgraph` console script，统计完整加载并执行一次查询的总耗时。
- 测量命令等价于：
```bash
clang-callgraph <path-to-cpython>/compile_commands.json \
  --library_path <local-libclang-dir> \
  --lookup 'list_iter(int *)'
```

### 结果
- 按上述真实安装口径重新测得冷启动总耗时：`16.808s`
- 该结果已包含完整 `compile_commands.json` 加载过程，低于你要求的 `30s`。

## Step 6：加载完成后输出统计摘要
### 新增输出
加载完成后会打印：
- `files`：加载的编译单元数量
- `functions`：加载得到的函数键数量（`FULLNAMES` 键数）
- `edges`：调用边数量
- `seconds`：本次加载耗时
- `cache`：是否命中缓存（`yes/no`）

示例：
```text
load summary: files=355, functions=15765, edges=35327, seconds=0.053, cache=yes
```

### 加载输出收敛
- 已去掉加载过程中逐条打印的 Clang 诊断详情，例如：
  - `('diags', ...)`
  - `fixits/location/ranges/severity/spelling`
- 进度显示现已按常见 CLI 规则调整：
  - 仅在 `stderr.isatty()` 时启用动态进度
  - 动态进度走 `stderr`
  - 使用限频刷新
  - 使用 `\r` + `\x1b[2K` 清整行
  - 每次按当前终端宽度裁剪
- 非 TTY / 管道 / 重定向场景下，不再刷逐文件进度，只保留：
  - `reading source files...`
  - `load summary: ...`

### 缓存目录策略
- 缓存现已写入被分析项目目录下的专属目录：`.clang-callgraph-cache/`
- 示例：
```text
<target-project>/.clang-callgraph-cache/<cache-key>.json
```
- OCaml 场景中曾出现重复边，但确认是旧缓存污染导致；在提升 `CACHE_VERSION` 并清除旧缓存后，冷加载结果恢复正常。

### 验证
- 新增摘要与缓存目录调整后，`verify_cache_results.py` 仍然通过：
  - `ok lookup,filter,ignore,ref,lookup_token`
- 说明新增摘要和缓存目录调整没有改变查询结果输出内容本身。

## 当前效率结论
- Index 复用优化已完成并验证。
- 缓存方向已在 `--lookup` 路径达到显著收益，且查询结果一致。
- 距离目标方面：
  - 冷启动 `16.808s`，已达到 30s 内目标。
  - 缓存启动 `0.175s`，已达到 2s 内目标。

## 后续计划
1. 把 `? / ! / &` 的验证从交互式 REPL 改成可超时、可单次退出的自动化验证。
2. 补充更多函数样本与参数组合，确认缓存图结构在不同查询路径下都不改变最终结果。
3. 如果继续优化，只做查询路径或缓存序列化体积方向，不再混入多项改动。
