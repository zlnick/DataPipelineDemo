# termsrv-patches/ —— 术语服务器的**平台扩展**（overlay）

这里的文件与 `termsrv/` 子模块（`zlnick/iris-terminology-server`，分支 `demo-community`）**同路径**，
由 `tools/termsrv_apply_patches.sh` 在子模块就绪后**覆盖进去**（幂等，内容相同则跳过）。

| 文件 | 作用 |
|---|---|
| `iris/src/Terminology/Mapping/CodeMap.cls` | 术语转换映射表（术语服务器 = 唯一事实源） |
| `iris/src/Terminology/Production/API.cls` | `/terminology/mapping/*` REST（lookup / availability / coverage / entries upsert） |

为什么内置一份：子模块远端可能落后或缺失这两个文件（历史情况：曾长期只在本地未提交），
若没有它们，构建出的术语服务器**没有映射能力**，`data/seeds/term_map_seed.json` 也就无处导入。
内置 overlay 让主仓库自给自足：`bash tools/setup.sh` 后术语转换立即可用。
