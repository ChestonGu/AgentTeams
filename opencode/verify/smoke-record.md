# T10 冒烟记录

**日期** 2026-09-01 · **结果** ✅ SMOKE PASSED（六步 18 项检查全过，生产 bucket 无残留）

## 环境

| 项 | 值 |
|---|---|
| 执行机 | extvdiadmin@10.254.254.105（Ubuntu 24.04，k8s 运维节点） |
| MinIO | `minio-sim`（ns `s3-sim`，即本集群 AgentTeams controller 实际使用的存储，`AGENTTEAMS_FS_ENDPOINT=http://minio-sim.s3-sim.svc.cluster.local:9000`）；节点外经 NodePort `http://10.254.254.105:30833` 访问 |
| 凭据 | `minio-sim-root` / `MinioSim#2026`（取自 agentos ns 真实 worker pod env，与 controller secret 同源） |
| mc | 从 worker pod `kubectl cp` 提取的真身 `mc.bin`（`/usr/local/bin/mc` 是 mc-wrapper），版本 `RELEASE.2025-08-13T08-35-41Z` |
| python3 / jq | Ubuntu 24.04 自带 |
| 集群侧部署 | ns `agentos`：controller v1.2.2.3v1 / manager / synapse / 6 个 copaw Worker CR（`AGENTTEAMS_RUNTIME=k8s`）——本次冒烟不触碰它们 |

## 执行方式

```bash
# 隔离参数：team=smoke-<ts>（不存在于 controller，纯对象键隔离），worker=smoke-worker
AGENTTEAMS_FS_ENDPOINT=http://10.254.254.105:30833 \
AGENTTEAMS_FS_ACCESS_KEY=<AK> AGENTTEAMS_FS_SECRET_KEY=<SK> \
bash verify/smoke.sh --alias smoke --bucket agentteams-storage \
     --team smoke-$(date +%s) --worker smoke-worker
```

环境注入走契约 §1 的 **local 模式三元组**（ENDPOINT/AK/SK → mc_sync 内部 `mc alias set`），
模拟沙箱冷启动路径；alias 三模式中的 k8s 模式由生产 worker pod 已验证（MC_HOST 由
mc-wrapper 维护），不在本次范围。

## 六步结果

| 步骤 | 检查项 | 结果 |
|---|---|---|
| 1 模板上 MinIO | skills + AGENTS.md mirror | ✅ |
| 2 leader 建任务 | meta.json(status=assigned) + spec.md | ✅ |
| 3 拒绝路径 | 冒充 ack 拒绝 / 未 ack 即 submit 拒绝 | ✅ ✅ |
| 3b happy path | check→assigned、ack 输出含 spec 全文、`assigned→in_progress`、deliverable、submit | ✅×4 |
| 5 Leader 读回 | result.md **逐字节一致**、meta.status=submitted、assigned_to 未变、submitted_at 有值、spec.md 存活（leader 所有） | ✅×5 |
| 6 filesync 四 action | push progress/、stat exists、list、pull 拾取远端漂移 | ✅×4 |
| 清理 | teams/、agents/ 无 smoke 残留对象 | ✅ ✅ |

## 发现并修复的问题（→ 契约 v1.0.1）

首轮冒烟 9 项 FAIL，根因一个：**taskflow `--sync` 默认值是 `none`**。
smoke/技能文档按部署形态裸调 `taskflow check <id>`（不带 `--sync`），
导致 check/ack/submit 全部不 pull，本地无 meta.json 直接 `file not found`
（步骤 1/2/3/6 因不走 taskflow 或走显式 env 而恰好全过，隔离出问题正在命令层）。

修复：`--sync` 默认 `none`→`mc`（copaw 的 taskflow 工具永远走 FileSync，
无本地形态，默认 mc 才是忠实迁移）。45 个单测均显式传 `--sync`，零回归；
README 本地示例补显式 `--sync none`；契约 §5.1 + §8 更新为 v1.0.1。

这同时验证了冒烟脚本的价值：**默认值错误单测抓不到**（所有单测都显式传参），
只有按 skill 文档的裸命令形态端到端跑一遍才暴露。

## 复现

```bash
# 前置：mc(alias smoke→NodePort 30833)、python3、jq；仓库内 verify/ + template/
cd opencode-worker-migration
AGENTTEAMS_FS_ENDPOINT=<endpoint> AGENTTEAMS_FS_ACCESS_KEY=<ak> \
AGENTTEAMS_FS_SECRET_KEY=<sk> bash verify/smoke.sh \
     --alias smoke --bucket agentteams-storage --team "smoke-$(date +%s)"
```
