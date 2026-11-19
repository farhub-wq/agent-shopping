# 沙箱执行镜像：Python 运行时 + 预置数据分析依赖，不含凭据与网络工具。
# 运行时仍由 DockerRunner 施加 network=none、只读根文件系统、CPU/内存/PID 上限；
# 依赖只在构建期安装，运行期无网络（network=none）无法再装包，避免供应链漂移。
FROM python:3.12-alpine

# 受限网络可在构建时传内网 pip 源，构建参数不写入镜像运行环境。
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_TRUSTED_HOST=

WORKDIR /tmp

# numpy/pandas 均提供 cp312 musllinux 轮子，alpine 无需编译工具链。
# 版本固定：镜像语义由 digest 固定，但锁定依赖版本可避免重新构建时依赖漂移；
# 新增业务依赖应改这里并重新构建、更新 SANDBOX_IMAGE digest，不在运行期 pip install。
RUN set -eux; \
    PIP_ARGS="--index-url ${PIP_INDEX_URL}"; \
    if [ -n "${PIP_TRUSTED_HOST}" ]; then PIP_ARGS="$PIP_ARGS --trusted-host ${PIP_TRUSTED_HOST}"; fi; \
    pip install --no-cache-dir ${PIP_ARGS} \
        "numpy==2.1.3" \
        "pandas==2.2.3"

CMD ["python", "-I"]
