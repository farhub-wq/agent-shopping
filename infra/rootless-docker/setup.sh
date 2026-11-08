#!/usr/bin/env bash
# 宿主侧一次性初始化：rootless dockerd + gVisor（runsc）。
#
# 目的：让 sandbox-runner 被攻破时拿到的是普通用户权限而非宿主 root，
# 并用 gVisor 的用户态内核收敛共享宿主内核的 CVE 面。
# 仅支持 Debian/Ubuntu（apt），需要 root 或 sudo 执行，可重复运行（幂等）。
#
# 用法：
#   sudo bash infra/rootless-docker/setup.sh
#   SANDBOX_USER=sandbox sudo -E bash infra/rootless-docker/setup.sh
set -euo pipefail

SANDBOX_USER="${SANDBOX_USER:-sandbox}"
RUNSC_RUNTIME="runsc"

log() { printf '[setup-sandbox-host] %s\n' "$*"; }
die() { printf '[setup-sandbox-host] ERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "必须以 root 或 sudo 执行"
command -v apt-get >/dev/null 2>&1 || die "仅支持 Debian/Ubuntu（未找到 apt-get）"
command -v python3 >/dev/null 2>&1 || die "需要 python3 来合并 daemon.json"

# 以沙箱用户身份执行命令：rootless dockerd 的 socket 与 systemd 用户服务都挂在该用户下。
as_user() {
  local home uid
  home="$(getent passwd "$SANDBOX_USER" | cut -d: -f6)"
  uid="$(id -u "$SANDBOX_USER")"
  sudo -u "$SANDBOX_USER" env HOME="$home" XDG_RUNTIME_DIR="/run/user/$uid" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" bash -lc "$*"
}

if ! id -u "$SANDBOX_USER" >/dev/null 2>&1; then
  log "创建专用非 root 用户 $SANDBOX_USER"
  useradd -m -s /bin/bash "$SANDBOX_USER"
fi

# linger 让用户级 dockerd 在无人登录时也能常驻，机器重启后自动拉起。
log "开启 linger"
loginctl enable-linger "$SANDBOX_USER"

log "安装 rootless Docker 依赖（uidmap / dbus-user-session / rootless extras）"
install -m 0755 -d /etc/apt/keyrings
if [ ! -f /etc/apt/keyrings/docker.asc ]; then
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
fi
# shellcheck disable=SC1091
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update -qq
apt-get install -y -qq uidmap dbus-user-session docker-ce-rootless-extras

# 安装 gVisor：runsc 作为 Docker 的一个 runtime 注册，容器内核调用走用户态实现。
if ! command -v runsc >/dev/null 2>&1; then
  log "安装 gVisor runsc"
  curl -fsSL https://gvisor.dev/archive.key | gpg --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" \
    > /etc/apt/sources.list.d/gvisor.list
  apt-get update -qq
  apt-get install -y -qq runsc
fi
RUNSC_BIN="$(command -v runsc)" || die "runsc 安装后仍不在 PATH"
log "runsc: $RUNSC_BIN"

SANDBOX_HOME="$(getent passwd "$SANDBOX_USER" | cut -d: -f6)"
SANDBOX_UID="$(id -u "$SANDBOX_USER")"
# rootless socket 属主是沙箱用户：runner 容器必须以同一 uid/gid 运行，
# 否则容器内 root(uid 0) 对 socket 无权限，会得到 EACCES。
SANDBOX_GID="$(id -g "$SANDBOX_USER")"
SOCKET="/run/user/$SANDBOX_UID/docker.sock"

if [ ! -S "$SOCKET" ]; then
  log "以 $SANDBOX_USER 安装并启动 rootless dockerd"
  as_user "dockerd-rootless-setuptool.sh install"
fi

# 把 runsc 注册到 rootless 守护进程配置里；合并而不是覆盖，保留已有设置。
DAEMON_JSON="$SANDBOX_HOME/.config/docker/daemon.json"
log "写入 runtime 配置: $DAEMON_JSON"
python3 - "$DAEMON_JSON" "$RUNSC_RUNTIME" "$RUNSC_BIN" <<'PY'
import json
import os
import sys

path, runtime_name, runsc_bin = sys.argv[1], sys.argv[2], sys.argv[3]
os.makedirs(os.path.dirname(path), exist_ok=True)
data = {}
if os.path.exists(path):
    with open(path, encoding="utf-8") as handle:
        text = handle.read().strip()
    if text:
        data = json.loads(text)
data.setdefault("runtimes", {})[runtime_name] = {"path": runsc_bin}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(data, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
PY
chown "$SANDBOX_USER":"$SANDBOX_USER" "$DAEMON_JSON"

log "重启用户级 dockerd 并等待 socket"
as_user "systemctl --user restart docker"
for _ in $(seq 1 30); do
  [ -S "$SOCKET" ] && break
  sleep 1
done
[ -S "$SOCKET" ] || die "rootless socket 未出现: $SOCKET"

# 实机校验：rootless 生效且 runsc 已注册，否则后续 runner 会 fail-closed。
as_user "docker info --format '{{.SecurityOptions}}'" | grep -q rootless \
  || die "dockerd 未以 rootless 模式运行"
as_user "docker info --format '{{.Runtimes}}'" | grep -q "$RUNSC_RUNTIME" \
  || die "runsc 未注册到 Docker runtime"

log "校验通过。把下面四行写入部署机 .env："
printf 'SANDBOX_ROOTLESS_SOCKET=%s\n' "$SOCKET"
printf 'SANDBOX_RUNTIME=%s\n' "$RUNSC_RUNTIME"
# runner 容器需要以沙箱用户的 uid/gid 运行才能访问 socket，否则会 EACCES。
printf 'SANDBOX_UID=%s\n' "$SANDBOX_UID"
printf 'SANDBOX_GID=%s\n' "$SANDBOX_GID"
log "容器内端点由 prod overlay 固定为 unix:///run/rootless/docker.sock，无需改动"
