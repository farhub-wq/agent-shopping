"""库存热点路径的 Redis Lua 脚本。

为什么必须用 Lua：`GET` 后再 `DECRBY` 是两个往返，中间窗口就是超卖窗口。
Lua 在 Redis 单线程内原子执行，要么全部扣减成功，要么一个都不扣。
"""

# KEYS[1] = 订单预占哈希键；KEYS[2..] = 各 SKU 可用库存键
# ARGV[1] = 预占 TTL 秒；ARGV[2] = SKU 数量；ARGV[3..] = 对应扣减数量
RESERVE_MULTI = """
local order_key = KEYS[1]
local ttl = tonumber(ARGV[1])
local n = tonumber(ARGV[2])

for i = 1, n do
  local available = tonumber(redis.call('GET', KEYS[i + 1]) or '-1')
  local need = tonumber(ARGV[i + 2])
  if available < need then
    return {0, i, available}
  end
end

for i = 1, n do
  local need = tonumber(ARGV[i + 2])
  redis.call('DECRBY', KEYS[i + 1], need)
  redis.call('HSET', order_key, KEYS[i + 1], need)
end
redis.call('EXPIRE', order_key, ttl)
return {1, 0, 0}
"""

# KEYS[1] = 订单预占哈希键。提交只清理预占标记，库存已在预占时扣减。
COMMIT_ORDER = """
redis.call('DEL', KEYS[1])
return 1
"""

# KEYS[1] = 订单预占哈希键；KEYS[2..] 为可选的库存键（为空时按哈希内字段回补）
RELEASE_ORDER = """
local order_key = KEYS[1]
local entries = redis.call('HGETALL', order_key)
local released = 0
for i = 1, #entries, 2 do
  local field = entries[i]
  local qty = tonumber(entries[i + 1])
  redis.call('INCRBY', field, qty)
  released = released + 1
end
redis.call('DEL', order_key)
return released
"""

# KEYS[1] = 秒杀库存键；KEYS[2] = 已购用户集合；ARGV[1] = 用户标识；ARGV[2] = 每人限购
SEC_KILL = """
local stock_key = KEYS[1]
local users_key = KEYS[2]
local user = ARGV[1]
local limit = tonumber(ARGV[2])

if redis.call('SISMEMBER', users_key, user) == 1 then
  return {0, 'DUPLICATE', -1}
end

local stock = tonumber(redis.call('GET', stock_key) or '0')
if stock <= 0 then
  return {0, 'SOLD_OUT', 0}
end

redis.call('DECRBY', stock_key, 1)
redis.call('SADD', users_key, user)
redis.call('EXPIRE', users_key, 86400)
return {1, 'OK', stock - 1}
"""

# 从数据库全量重建热点库存：仅用于启动对账或人工修复
SYNC_STOCK = """
redis.call('SET', KEYS[1], ARGV[1])
return tonumber(ARGV[1])
"""
