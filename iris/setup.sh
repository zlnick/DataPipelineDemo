#!/bin/sh
# =====================================================================
# AI 数据自动化转换 Demo - IRIS 统一初始化脚本
# 由容器入口 /iris-main 通过 -a /shared/setup.sh 在 IRIS 启动后执行：
#   1. 配置 superuser/SYS 登录凭据（复用 init-password.sh）
#   2. 搭建 FHIR Server（FHIRSERVER Foundation namespace + 核心 R4 endpoint）
#   （后续 Part：编译 Production 组件 + 建目标表 + 加载 FHIR 示例数据 + 启动 Production）
# =====================================================================
set -eu

instance="${ISC_PACKAGE_INSTANCENAME:-IRIS}"

# 1. 凭据初始化（superuser / SYS）
echo "=== [setup] 凭据初始化 ==="
sh /shared/init-password.sh

# 2. 搭建 FHIR Server
# 参照 CQFHIRDockerRelease 项目 init.sh 的 FHIR Server 安装方法：
#   - HSLIB 下创建 FHIRSERVER Foundation namespace
#   - 安装 FHIR 命名空间所需元素 + FHIR 服务实例（IRIS 自带核心 R4，不导入自定义 profile）
# 注意（已确认方案 A）：IRIS FHIR Server 对资源读写默认要求认证（Basic Auth: superuser/SYS），
#       仅 /metadata（CapabilityStatement）匿名公开；Demo 中数据源访问一律携带认证。
echo "=== [setup] 搭建 FHIR Server（FHIRSERVER namespace + 核心 R4 endpoint）==="
cat << 'EOF' > /tmp/setup_fhir.os
zn "%SYS"
set ns = "FHIRSERVER"
if '##class(%SYS.Namespace).Exists(ns) zn "HSLIB" do ##class(HS.Util.Installer.Foundation).Install(ns)
zn ns
do ##class(HS.FHIRServer.Installer).InstallNamespace()
set appKey = "/csp/healthshare/fhirserver/fhir/r4"
set strategyClass = "HS.FHIRServer.Storage.JsonAdvSQL.InteractionsStrategy"
set metadataPackages = $lb("hl7.fhir.r4.core@4.0.1")
try { do ##class(HS.FHIRServer.Installer).InstallInstance(appKey, strategyClass, metadataPackages) write "INSTALL_INSTANCE_OK",! } catch ex { write "INSTALL_INSTANCE_SKIPPED",! }
zn "%SYS"
if ##class(Security.Applications).Get(appKey, .props) { set props("AutheEnabled") = 8288 set props("MatchRoles") = ":%All" do ##class(Security.Applications).Modify(appKey, .props) }
write "FHIR_SERVER_SETUP_DONE", !
halt
EOF

/usr/irissys/bin/iris session "$instance" -U %SYS < /tmp/setup_fhir.os

# 2b. 搭建第二个 FHIR 存储库（DemoFHIR namespace + 独立仓库端点）
# 目的：与上面 FHIRSERVER 完全同构的**第二个 FHIR 仓库**（独立命名空间 + 独立数据库 + 独立端点），
#       供演示「多 FHIR 仓库并存 / 数据互不干扰」。
# 官方安装序列（与 FHIRSERVER 一字不差，只是换了命名空间与端点路径）：
#   Foundation.Install(ns)  → 建库（/dur/irissys/mgr/<ns>，随 ISC_DATA_DIRECTORY 持久化）+ 建命名空间
#                             （含 Ensemble 映射、Portal CSP 应用、FoundationProduction 模板、FHIR 元数据包）
#   InstallNamespace()      → 在**当前命名空间**内安装 FHIR Server 支持（编译 HSFHIR.* 存储类、建表）
#   InstallInstance(...)    → 注册 FHIR 仓库端点（CSP 应用 + DispatchClass=HS.FHIRServer.RestHandler），
#                             pCreateDatabases=1 会自动建 <ns>X0001R / <ns>X0001V 两个数据仓库库
# 幂等：命名空间已存在则跳过 Foundation.Install；端点已存在时 InstallInstance 异常被捕获跳过，
#       随后只对齐该端点自身的认证（AutheEnabled/MatchRoles，与既有 FHIR 端点同口径）——
#       ⚠ 只作用于本步骤新建的端点应用，绝不触碰任何既有 Web Application（门户/登录等）。
# 说明：可在运行中的实例上重复执行同一段脚本（不重启 IRIS）：`python3 tools/create_fhir_repo.py`
echo "=== [setup] 搭建第二个 FHIR 存储库（DemoFHIR namespace + 独立仓库端点） ==="
cat << 'EOF' > /tmp/setup_demofhir.os
zn "%SYS"
set ns = "DEMOFHIR"
set appKey = "/csp/healthshare/demofhir/fhir/r4"
set strategyClass = "HS.FHIRServer.Storage.JsonAdvSQL.InteractionsStrategy"
set metadataPackages = $lb("hl7.fhir.r4.core@4.0.1")
write "[demofhir] 前置：命名空间=", ##class(%SYS.Namespace).Exists(ns), " 库=", ##class(Config.Databases).Exists(ns), " CSP应用=", ##class(Security.Applications).Exists(appKey), !
if '##class(%SYS.Namespace).Exists(ns) { zn "HSLIB" do ##class(HS.Util.Installer.Foundation).Install(ns) zn "%SYS" write "[demofhir] Foundation.Install 完成，新建库=", ##class(Config.Databases).Exists(ns), ! }
zn ns
do ##class(HS.FHIRServer.Installer).InstallNamespace()
write "[demofhir] InstallNamespace 完成（当前命名空间=", $namespace, "）", !
try { do ##class(HS.FHIRServer.Installer).InstallInstance(appKey, strategyClass, metadataPackages) write "[demofhir] InstallInstance 完成", ! } catch ex { write "[demofhir] InstallInstance 跳过/异常: ", ex.DisplayString(), ! }
zn "%SYS"
if ##class(Security.Applications).Get(appKey, .props) { set props("AutheEnabled") = 8288 set props("MatchRoles") = ":%All" do ##class(Security.Applications).Modify(appKey, .props) write "[demofhir] CSP 应用认证已设置（AutheEnabled=8288, MatchRoles=:%All）", ! }
write "[demofhir] 结果：命名空间=", ##class(%SYS.Namespace).Exists(ns), " 库=", ##class(Config.Databases).Exists(ns), " CSP应用=", ##class(Security.Applications).Exists(appKey), !
write "[demofhir] DEMOFHIR_FHIR_REPO_DONE", !
halt
EOF

/usr/irissys/bin/iris session "$instance" -U %SYS < /tmp/setup_demofhir.os

# 3. 编译 Production 组件类（按依赖顺序逐个编译，避免 LoadDir 的编译顺序竞态）
echo "=== [setup] 编译 Production 组件类 ==="
/usr/irissys/bin/iris session "$instance" -U USER <<'EOF'
do $SYSTEM.OBJ.Load("/shared/src/demo/FHIRRequest.cls", "ck")
do $SYSTEM.OBJ.Load("/shared/src/demo/TransformResult.cls", "ck")
do $SYSTEM.OBJ.Load("/shared/src/demo/FHIRTransformHelper.cls", "ck")
do $SYSTEM.OBJ.Load("/shared/src/demo/FHIRSyncService.cls", "ck")
do $SYSTEM.OBJ.Load("/shared/src/demo/FHIRService.cls", "ck")
do $SYSTEM.OBJ.Load("/shared/src/demo/TransformProcess.cls", "ck")
do $SYSTEM.OBJ.Load("/shared/src/demo/TargetOperation.cls", "ck")
do $SYSTEM.OBJ.Load("/shared/src/demo/PipelineGenerator.cls", "ck")
do $SYSTEM.OBJ.Load("/shared/src/demo/PipelineQuery.cls", "ck")
do $SYSTEM.OBJ.Load("/shared/src/demo/WSDLImporter.cls", "ck")
write "COMPILE_DONE", !
halt
EOF

# 4. 配置 JDBC 数据源 localTarget（通用 DSN：无 jdbc_url 时的历史兜底，指向 USER namespace）
#    - Name=连接逻辑名（SQL Operation 的 DSN 引用它）
#    - DSN=JDBC URL（必须含冒号，EnsLib.SQL.OutboundAdapter 依此判断走 JDBC 而非 ODBC）
#    - isJDBC=1 + driver/URL/Usr/pwd 完整配置
#    ⚠ 演示默认口径（2026-09-16）：**SQL 源 = USER、SQL 目标 = CLINIC** —— 源/目标的 DSN 都由
#      登记 jdbc_url 的命名空间推导（`…/<ns>` → DSN `<ns>`，见 backend/services/jdbc_dsn.py），
#      `localTarget` 只在"没有 jdbc_url"时才被引用。
echo "=== [setup] 配置 JDBC 数据源 localTarget ==="
/usr/irissys/bin/iris session "$instance" -U %SYS <<'EOF'
// 幂等：连接已存在则跳过（注意：iris session stdin 不支持多行 if，且 SQL 字面量需用参数绑定避免单引号被解析）
set rs = ##class(%SQL.Statement).%New()
do rs.%Prepare("SELECT connection_name FROM %Library.sys_SQLConnection WHERE connection_name=?")
set r = rs.%Execute("localTarget")
set found = r.%Next()
if found { write "DSN_localTarget_EXISTS", ! }
if found=0 { set c = ##class(%SQLConnection).%New() set c.Name = "localTarget" set c.DSN = "jdbc:IRIS://127.0.0.1:1972/USER" set c.Usr = "superuser" set c.pwd = "SYS" set c.driver = "com.intersystems.jdbc.IRISDriver" set c.URL = "jdbc:IRIS://127.0.0.1:1972/USER" set c.isJDBC = 1 set sc = c.%Save() write "DSN_localTarget_CREATED: ", $system.Status.GetErrorText(sc), ! }
halt
EOF

# 5. 创建 CLINIC 命名空间与数据库（SQL 演示源：患者/就诊/诊断/药嘱四表）
# 背景：CLINIC 库此前靠手工创建且不持久 —— 容器重建后即消失，SQL 源 BS 报
#       ErrOutConnectFailed: Access Denied（JDBC 连不存在的 namespace）。
# 说明：数据库目录必须预先存在（IRIS 不自动建目录），且放在 ISC_DATA_DIRECTORY 内以持久化。
echo "=== [setup] 创建 CLINIC 命名空间与数据库 ==="
/usr/irissys/bin/iris session "$instance" -U %SYS <<'EOF'
set ex = ##class(Config.Namespaces).Exists("CLINIC")
if ex { write "CLINIC_NS_EXISTS", ! }
if 'ex { set dbdir = "/dur/irissys/mgr/CLINIC/" do ##class(%Library.File).CreateDirectoryChain(dbdir) set d = ##class(SYS.Database).%New() set d.Directory = dbdir set sc1 = d.%Save() set props("Directory") = dbdir set sc2 = ##class(Config.Databases).Create("CLINIC", .props) set np("Globals") = "CLINIC" set sc3 = ##class(Config.Namespaces).Create("CLINIC", .np) write "CLINIC_NS_CREATED: ", sc1, "/", sc2, "/", sc3, ! }
halt
EOF

# 6. 配置 JDBC 数据源 CLINIC（CLINIC 命名空间演示源，SQL 源 BS 轮询四表用；同 localTarget 模式）
echo "=== [setup] 配置 JDBC 数据源 CLINIC ==="
/usr/irissys/bin/iris session "$instance" -U %SYS <<'EOF'
set rs = ##class(%SQL.Statement).%New()
do rs.%Prepare("SELECT connection_name FROM %Library.sys_SQLConnection WHERE connection_name=?")
set r = rs.%Execute("CLINIC")
set found = r.%Next()
if found { write "DSN_CLINIC_EXISTS", ! }
if found=0 { set c = ##class(%SQLConnection).%New() set c.Name = "CLINIC" set c.DSN = "jdbc:IRIS://127.0.0.1:1972/CLINIC" set c.Usr = "superuser" set c.pwd = "SYS" set c.driver = "com.intersystems.jdbc.IRISDriver" set c.URL = "jdbc:IRIS://127.0.0.1:1972/CLINIC" set c.isJDBC = 1 set sc = c.%Save() write "DSN_CLINIC_CREATED: ", $system.Status.GetErrorText(sc), ! }
halt
EOF

echo "=== [setup] IRIS 初始化完成 ==="
