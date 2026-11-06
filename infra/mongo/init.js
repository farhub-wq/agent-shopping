// Harness 的文档型数据：记忆与 Agent 轨迹。
// Mongo 没有 schema 迁移，这里预建集合与索引；服务内首次写入时也会幂等补建同样索引。
db = db.getSiblingDB("shop_harness");

db.createCollection("memory_records");
// 去重时按 用户+类型 取最近记录；列表与召回按时间排序。
db.memory_records.createIndex({ user_id: 1, kind: 1, created_at: -1 });
db.memory_records.createIndex({ user_id: 1, created_at: -1 });

db.createCollection("agent_traces");
// 一次 run 对应一个文档，run_id 唯一保证同一运行只有一条轨迹。
db.agent_traces.createIndex({ run_id: 1 }, { unique: true });
