# AI 数据自动化转换 Demo - 后端 API 服务镜像
FROM python:3.11-slim

# 安装 JVM（JayDeBeApi 调 JDBC 需要；Debian trixie 提供 openjdk-21）
RUN apt-get update && apt-get install -y --no-install-recommends openjdk-21-jre-headless \
    && rm -rf /var/lib/apt/lists/*

# 工作目录
WORKDIR /app

# 先复制依赖清单并安装，充分利用 Docker 层缓存
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# 复制后端代码与初始化脚本
COPY backend/ /app/backend/
COPY init_data.py /app/init_data.py

# 使 backend 可作为 Python 包被 import（初始化脚本位于 /app 下）
ENV PYTHONPATH=/app

# 启动流程：先建目标表（init_data.py，DROP+CREATE 幂等），最后启动 Flask API 服务。
# ⚠ 启动**不灌** FHIR 演示数据（2026-09-16 起）：演示数据一律"用时现造"
#   （tools/gen_test_patient.py / generate_mock_data.py --fhir N）；
#   需要"库里本就有历史存量"时手动跑 tools/seed_fhir_demo.py（datakit 入口见 tools/datakit/README.md）。
CMD ["sh", "-c", "python /app/init_data.py && python /app/backend/app.py"]
