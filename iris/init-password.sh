#!/bin/sh
# =====================================================================
# IRIS Demo 登录凭据初始化：superuser / SYS
# 解决新版 IRIS 镜像对默认用户强制改密的问题（登录报 Password change required）
# 每次容器启动都执行（幂等），确保 down/up 后密码过期状态恢复时也能修复
# =====================================================================
set -eu
instance="${ISC_PACKAGE_INSTANCENAME:-IRIS}"

output="$(iris session "$instance" -U %SYS <<'EOF'
do ##class(Security.Users).UnExpireUserPasswords("*")

set rs = $SYSTEM.SQL.Execute("ALTER USER SuperUser PASSWORD 'SYS'")
if rs.%SQLCODE '= 0 { write "AlterSuperUser error: ",rs.%Message,! } else { write "AlterSuperUser OK",! }

kill props
set sc=##class(Security.Users).Get("SuperUser",.props)
set props("ChangePassword")=0
write "SuperUser=",##class(Security.Users).Modify("SuperUser",.props),!

kill props
set sc=##class(Security.Users).Get("_SYSTEM",.props)
set props("ChangePassword")=0
write "_SYSTEM=",##class(Security.Users).Modify("_SYSTEM",.props),!

kill props
set sc=##class(Security.Users).Get("CSPSystem",.props)
set props("ChangePassword")=0
write "CSPSystem=",##class(Security.Users).Modify("CSPSystem",.props),!
halt
EOF
)"

printf '%s\n' "$output"
if printf '%s' "$output" | grep -q '<'; then
  exit 1
fi
# 记录已执行（仅作标记；不用于跳过，保证每次启动都重新确保凭据有效）
touch /dur/.change-password-cleared
