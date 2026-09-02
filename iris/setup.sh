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
/usr/irissys/bin/iris session "$instance" -U %SYS <<'EOF'
zn "%SYS"

// 创建 FHIRSERVER Foundation namespace（幂等）
set ns = "FHIRSERVER"
if '##class(%SYS.Namespace).Exists(ns) {
    zn "HSLIB"
    do ##class(HS.Util.Installer.Foundation).Install(ns)
}

zn ns

// 安装 FHIR 命名空间所需元素
do ##class(HS.FHIRServer.Installer).InstallNamespace()

// 安装 FHIR 服务实例（核心 R4，不导入自定义 profile）
// Try/Catch 保证幂等：endpoint 已存在时忽略"已存在"错误（容器重启后不会重复安装）
set appKey = "/csp/healthshare/fhirserver/fhir/r4"
set strategyClass = "HS.FHIRServer.Storage.JsonAdvSQL.InteractionsStrategy"
set metadataPackages = $lb("hl7.fhir.r4.core@4.0.1")
try {
    do ##class(HS.FHIRServer.Installer).InstallInstance(appKey, strategyClass, metadataPackages)
    write "INSTALL_INSTANCE_OK", !
} catch ex {
    write "INSTALL_INSTANCE_SKIPPED: ", $system.Status.GetErrorText(ex.AsStatus()), !
}

// 确保 Web Application 开启 Basic Password 认证与 %All 角色权限，解决 HTTP 401/404 认证匹配异常
zn "%SYS"
if ##class(Security.Applications).Get(appKey, .props) {
    set props("AutheEnabled") = 8288
    set props("MatchRoles") = ":%All"
    do ##class(Security.Applications).Modify(appKey, .props)
}

write "FHIR_SERVER_SETUP_DONE", !
halt
EOF

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
write "COMPILE_DONE", !
halt
EOF

# 4. 配置 JDBC 数据源 localTarget（SQL Operation 通过 JDBC 写入 USER namespace 目标表）
#    - Name=连接逻辑名（SQL Operation 的 DSN 引用它）
#    - DSN=JDBC URL（必须含冒号，EnsLib.SQL.OutboundAdapter 依此判断走 JDBC 而非 ODBC）
#    - isJDBC=1 + driver/URL/Usr/pwd 完整配置
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

echo "=== [setup] IRIS 初始化完成 ==="
