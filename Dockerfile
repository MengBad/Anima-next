# Anima 独立进程镜像（Linux only）
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

# 构建依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装 Python 依赖（利用层缓存；不复制源码也可安装可编辑包）
COPY pyproject.toml requirements.txt ./
COPY anima ./anima
RUN pip install --upgrade pip && pip install -e .

# 运行时目录
RUN mkdir -p data data/output plugins \
    && useradd -m -u 1000 anima

USER anima

# 3001 供同机 NapCat 反向 WS 连接；8080 可选 WebUI/console
EXPOSE 3001 8080

VOLUME ["/app/data", "/app/config.yaml"]

ENTRYPOINT ["python", "-m", "anima"]
CMD ["start", "--config", "/app/config.yaml"]
