# 沙箱执行镜像：只装 Python 运行时，不含任何业务依赖与凭据。
# 运行时仍由 SandboxManager 施加 network=none、只读根文件系统、CPU/内存/PID 上限。
FROM python:3.12-alpine

WORKDIR /tmp
CMD ["python", "-I"]
