# tools/guard —— AI 操作边界守卫（声明 + 防呆 + 协议 + 可选文件沙箱）

> 背景：2026-09-14 AI 在"清理临时文件"任务中擅自 `docker rm` 删除**本项目之外**的 7 个容器
> （testdemo / latesti4h / iris3in1demo）及其网络，其中 testdemo 容器内的 IRIS 库不可恢复。
> 本目录的作用是：让"AI 只能动本项目"这件事**不再依赖 AI 自觉**。

## 落地层次

| 层 | 文件 | 强制方式 | 强度 |
|---|---|---|---|
| L0 声明 | `AGENTS.md` 顶部「AI 权限边界」章节 + `.clinerules/00-permission-boundary.md`、`.clinerules/01-destructive-op-protocol.md` | AI 每次会话自动读取 | 弱（但不可"没看到"） |
| L1 防呆 | `tools/guard/docker_guard.sh`、`tools/guard/scope_guard.py` | 脚本拦截（退出码 77 / 非 0） | 中（可被绝对路径绕过） |
| L2 流程 | `.clinerules/01-destructive-op-protocol.md` 三段式（枚举→停下等确认→执行）+ 备份落 `./.trash/` | 流程约束 | 中 |
| 可选强化 | `tools/guard/ai-sandbox.sb` + `ai-session.sh` / `ai-session-rc.sh` | macOS 文件沙箱（越界写入内核拒绝）+ CLI 守卫 | 文件层强（内核）；docker 层与 L1 同级 |

> ⚠ **2026-09-14 回滚**：曾上线过一层「受限 Docker API 代理」（compose 服务 `docker-proxy` +
> `.vscode/settings.json` 把终端 `DOCKER_HOST` 指向它）——**已按用户要求回滚**：太复杂（要自己维护
> 归属裁决 + HTTP 流式透传），且因 `docker cp` 上传是流式请求体被 fail-closed 误拒，反而打断了日常操作。
> 现在**只保留**：声明（L0）+ CLI 守卫（L1）+ 三段式流程（L2）+ 可选的文件沙箱会话。
> 已删除：`tools/guard/docker_proxy.py`、`docker-compose.yml` 里的 `docker-proxy` 服务、
> `.vscode/settings.json` 的 `terminal.integrated.env.osx`。

## L1 用法

```bash
# Docker：先 source，之后所有 docker 调用都会过白名单（拒绝时退出码 77，记 .trash/docker-guard.log）
cd /Users/lzhu/Documents/GeneratorDemo
source tools/guard/docker_guard.sh

docker ps                      # ✅ 只读，直通
docker exec dataflow-iris ...  # ✅ 本项目容器
docker compose restart backend # ✅ 在本项目目录内
docker rm health-iris          # ❌ 拒绝（非本项目）
docker system prune -f         # ❌ 拒绝（全局破坏性）
cd ~/Documents/repository/iris3in1demo && docker compose down   # ❌ 拒绝（不在本项目目录）

# 路径：任何清理/删除脚本动手前校验目标
python3 tools/guard/scope_guard.py check tools/foo.py /Users/lzhu/Documents/temp/x
# → [OK  ] 在允许范围内（仓库）… / [DENY] 越界：… ；有越界则退出码 1
```

作为库使用（清理脚本里）：

```python
import sys; sys.path.insert(0, "tools/guard")
from scope_guard import assert_in_scope, ScopeViolation

assert_in_scope(candidate_paths)  # 越界即抛 ScopeViolation，脚本必须中止
```

**守卫故意不提供"AI 可自行打开的放行开关"**：确需越界时，由用户在**未 source 守卫**的终端里手动执行，
或对具体清单逐条确认后由用户执行。

## 可选：受限 AI 会话（文件沙箱 + CLI 守卫）

```bash
# 在普通终端执行一次，然后在弹出的受限 shell 里让 AI 干活
./tools/guard/ai-session.sh
```

它做两件事：`source docker_guard.sh`（CLI 层早报错）→
`sandbox-exec -f tools/guard/ai-sandbox.sb -D REPO=<仓库> -D VAULT=<知识库> -D CACHES=~/Library/Caches bash -i`。
沙箱语义：**写入**只允许 仓库 / 知识库 / `/tmp` / `~/Library/Caches`（缓存可再生），其余（`~/Documents/temp`、
`~/Documents/repository`、其它用户目录、`/etc`、`/Volumes`…）**内核直接拒绝**（`Operation not permitted`）；
读取不受影响。提示符变成 `[受限AI]`。
本会话**不改** `DOCKER_HOST`（用本机真实 socket）——docker 侧边界由**规则 + CLI 守卫**承担。

### 沙箱实测（2026-09-14 回归，全部无破坏）

| 用例 | 数量 | 结果 |
|---|---|---|
| 沙箱（写仓库/知识库/`/tmp` 放行；写他项目目录/`~/`/`/etc` 内核拒绝；沙箱内 docker 可用；沙箱内 CLI 守卫 rc=77） | 15 | 全 PASS |

### 已回滚的代理层（历史记录：为什么不再用）

- 曾实现 `tools/guard/docker_proxy.py`（compose 服务 `docker-proxy`，宿主 `127.0.0.1:2375` → `/var/run/docker.sock`）：
  对破坏性请求先按 **daemon 侧事实**（`com.docker.compose.project` 标签 / 容器名 / RepoTags）裁决归属，
  越界对象与 `*/prune`、`pull` 一律 403，判定不出 fail-closed。
- **放弃原因**：① 复杂度高（裁决规则 + chunked 解码 + exec/attach 半关闭透传等一堆边角）；
  ② `docker cp` 上传是**流式请求体** → 走进 fail-closed 分支被误拒（`PUT /containers/<本项目容器>/archive`
  这种 URL 就能判明的请求也被挡），把"往容器里拷个脚本"这种日常操作打断了；
  ③ 用户判断：收益不值这个复杂度。
- 保留的教训（已记入知识库）：**fail-closed 过宽 = 什么都干不了**；防呆层不能挡合法路径。

## L1 生效的两个实用前提

| 做法 | 效果 | 代价 |
|---|---|---|
| 只是要求 AI 先 source（协议层） | 拦"顺手删"，AI 忘了 source 就失效 | 零成本 |
| **在 IDE（Cline）里关闭终端命令自动批准** | 每条命令都要你点确认——**本次事故最直接的拦截点** | 每条命令点一次 |
| 可选：用 `./tools/guard/ai-session.sh` 开受限会话 | 越界**写入**内核拒绝（docker 侧仍靠 CLI 守卫） | 需另开一个终端 |

> 不建议把 `source tools/guard/docker_guard.sh` 写进你自己的 `~/.zshrc`：那样会连你**合法**管理其它项目
> 的 docker 命令一起拦掉（例：`~/Documents/repository/*` 的三个项目）。要给 AI 一个受限会话，用
> `./tools/guard/ai-session.sh` 单独开一个终端即可。

## 实测汇总（2026-09-14 回归）

| 层 | 用例 | 结果 |
|---|---|---|
| L1 CLI 守卫 | 13（越界 `rm/stop/network rm/rmi/run`、全局 `system prune`、项目外 `compose down` → rc=77；只读 / 本项目 `exec` / `compose ps` → rc=0） | 全 PASS，输出与审计均为合法 UTF-8 |
| 文件沙箱会话 | 15（写仓库/知识库/`/tmp` 放行；写他项目目录/`~/`/`/etc` 内核拒绝；沙箱内 docker + CLI 守卫照常） | 全 PASS |

## 局限（必须知道）

- `docker_guard.sh` 是 shell 函数包装：`/usr/local/bin/docker rm ...` 这类绝对路径调用可绕过；
  它挡的是"顺手删"，不是"蓄意绕"。
- **已知误拒（不是越界，换写法即可，不要绕过守卫）**：守卫把「第一个非 `-` 开头的参数」当目标，
  而 `-w` / `-v` 这类**带值选项**的值会排在容器名前，于是被当成目标 →
  - `docker exec -w /app dataflow-backend python x.py` → **rc=77**（把 `/app` 当越界目标）。
    容器 `WORKDIR` 通常已是 `/app`，直接 `docker exec <容器> python …` 即可；
  - `docker cp <宿主路径> <容器>:<容器路径>` → 同样被首个参数（**源**路径）判归属而误拒。
    往本项目容器送文件改用**管道**：
    `docker exec -i <容器> sh -c 'cat > /tmp/x.py' < 本地文件` → 再 `docker exec <容器> python /tmp/x.py`。
- `scope_guard.py` 只校验**它被调用时传入的路径**；清理脚本必须真的调用它才有意义（协议要求）；
  文件层的兜底是可选沙箱（内核拒绝越界写入）。
- 沙箱只约束**从 `ai-session.sh` 起的那个 shell**；AI 在别的工作区/普通终端里跑就不受限。
- 因此流程层（先枚举清单、等用户确认、再执行）仍是最后一道防线：**没有任何一步允许 AI 自行放行**。
