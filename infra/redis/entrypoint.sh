#!/bin/sh
set -eu

# Redis 不再允许匿名连接：default 用户供业务服务使用，sandbox-runner 供 runner 使用。
# 两个密码都只从环境注入，不写进仓库与镜像层；生成后的 ACL 文件只对 redis 进程可读。
: "${REDIS_PASSWORD:?REDIS_PASSWORD 必须注入}"
: "${SANDBOX_REDIS_PASSWORD:?SANDBOX_REDIS_PASSWORD 必须注入}"

# 用 shell 变量展开替换占位符，避免密码里的 sed 元字符破坏 ACL 文件。
render() {
  line=$1
  case "$line" in
    *__REDIS_PASSWORD__*)
      line="${line%%__REDIS_PASSWORD__*}${REDIS_PASSWORD}${line#*__REDIS_PASSWORD__}"
      ;;
  esac
  case "$line" in
    *__SANDBOX_REDIS_PASSWORD__*)
      line="${line%%__SANDBOX_REDIS_PASSWORD__*}${SANDBOX_REDIS_PASSWORD}${line#*__SANDBOX_REDIS_PASSWORD__}"
      ;;
  esac
  printf '%s\n' "$line"
}

while IFS= read -r line; do
  render "$line"
done < /etc/redis/users.acl.template > /etc/redis/users.acl
chmod 600 /etc/redis/users.acl

exec redis-server --appendonly yes --aclfile /etc/redis/users.acl
