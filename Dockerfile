# qmdler — WebUI 模式
#
# 数据全部落在挂载卷里:
#   /config   配置、凭证 (600)、device.json    —— 必须持久化, 否则设备指纹每次重启都变
#   /data     SQLite 数据库
#   /music    下载目录

FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip wheel \
 && /opt/venv/bin/pip install .

# --------------------------------------------------------------------------- #

FROM python:3.12-slim

LABEL org.opencontainers.image.title="qmdler" \
      org.opencontainers.image.description="QQ 音乐歌单批量下载器（WebUI + TUI）" \
      org.opencontainers.image.licenses="GPL-3.0-or-later"

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    XDG_CONFIG_HOME=/config \
    XDG_DATA_HOME=/data \
    XDG_STATE_HOME=/data/state \
    QMDLER_SERVER__HOST=0.0.0.0 \
    QMDLER_SERVER__PORT=8770 \
    QMDLER_PATHS__SAVE_ROOT=/music

COPY --from=builder /opt/venv /opt/venv

RUN useradd --create-home --uid 1000 qmdler \
 && mkdir -p /config /data /music \
 && chown -R qmdler:qmdler /config /data /music

USER qmdler
WORKDIR /home/qmdler

VOLUME ["/config", "/data", "/music"]
EXPOSE 8770

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8770/api/health', timeout=4).status==200 else 1)"

ENTRYPOINT ["qmdler"]
CMD ["web"]
