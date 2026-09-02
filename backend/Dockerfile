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
COPY init_fhir_data.py /app/init_fhir_data.py

# 使 backend 可作为 Python 包被 import（初始化脚本位于 /app 下）
ENV PYTHONPATH=/app

# 启动流程：先建目标表、再加载 FHIR 示例数据，最后启动 Flask API 服务
CMD ["sh", "-c", "python /app/init_data.py && python /app/init_fhir_data.py && python /app/backend/app.py"]
