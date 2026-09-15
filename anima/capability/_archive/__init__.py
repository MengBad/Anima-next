"""capability 扩张期冻结归档（2026-09-12，队列①）。

内容：
- 8 个实现模块：``evolution_contract`` + ``runtime_*``（进化契约与运行时观测集群）；
- 16 个顶层 ``capability_*.py`` B5-merge 兼容 shim（当时把它们折叠进本包的过渡层）。

约定：
- 这是**只读冻结快照**，不是可运行包：模块间按当时的顶层 shim 路径互相 import，
  直接 import 会失败；复活（个别符号找回或整包恢复）需按 git history 重接 import。
- pytest 不收集此目录（tests/conftest.py 的 collect_ignore_glob 只作用于 tests/，
  本目录不在 testpaths 内，天然不参与收集）。
- 复活判定参考：若生产代码再次需要运行时观测/进化契约语义，应新开设计而非直接
  恢复本快照——它们的治理假设（Offline planner + 摘要链）已在 capability 留守模块
  （activation/staged/suspend/authorization/dispatch/intent/health_linker）中延续。
"""
