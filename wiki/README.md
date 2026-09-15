# HiClaw Wiki — 控制器运维与调优知识库

> 本目录沉淀 HiClaw 控制器（hiclaw-controller，Kubernetes operator）的**缺陷复盘、调优方法论与运维手册**。
> 结构设计：方法论（通用，可复用）→ 实例缺陷（本仓库）→ 实例参数（本仓库），三层闭环。

## 文档索引

| 文档 | 定位 | 内容 | 读者 |
|------|------|------|------|
| [kubernetes-controller-perf-sop.md](kubernetes-controller-perf-sop.md) | **通用方法论**（不绑定具体项目） | 任意 K8s CRD 控制器性能问题的完整排查链路：**现象 → 定位 → 常见问题的解法 → 推荐的实践**（四段式）；含 P-1~P-15 现象清单、五步定位法、C-1~C-15 问题模式解法表、B-1~B-12 设计实践、决策树与检查清单 | 任何 controller/operator 开发者 |
| [team-controller-defect-fixes.md](team-controller-defect-fixes.md) | **实例缺陷清单**（HiClaw 分支 `fix/team-cr-blocking-defects`） | F-01~F-14 缺陷→根因→优化方案→提交映射；与调研底稿编号对照；关键设计决策；未纳入分支的待办 | HiClaw 维护者 |
| [controller-tuning-sop.md](controller-tuning-sop.md) | **实例调优手册**（HiClaw 专属参数） | 调优参数总表（14 个 env + 内置常量）、症状识别矩阵（S-1~S-6）、日志/指标/pprof/bench 操作、决策树、运维动作（重试武装、CRD schema 核对、回滚矩阵） | HiClaw 运维/排障人员 |

## 阅读顺序建议

```
新读者（排查思路）: kubernetes-controller-perf-sop.md（先方法论，建立心智模型）
                     ↓
HiClaw 遇到具体问题: team-controller-defect-fixes.md（对照已知缺陷）
                     ↓
需要动手调参/运维:   controller-tuning-sop.md（参数与命令）
```

## 文档间引用关系

```
kubernetes-controller-perf-sop.md  (通用方法论)
  ├─ 实例映射表 ──→ team-controller-defect-fixes.md  (每个 C-x/B-x 对应哪个提交修复)
  └─ 实例映射表 ──→ controller-tuning-sop.md         (每个 C-x/B-x 对应哪个环境变量)
```

## 维护约定

- 新增控制器相关复盘/调优文档，优先放本目录并在此索引登记；
- 调研底稿（未整理的 `.issue/` 笔记）不入库，整理定稿后迁移至此；
- 提交前缀统一 `docs(wiki):`。
