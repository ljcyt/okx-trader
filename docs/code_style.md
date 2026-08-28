# 代码风格（15 条）

源自本仓库前身的 CONTRIBUTING，保留仍适用于交易系统的部分：

1. 自解释代码优先；注释只说明代码无法表达的结构/原因，不复述代码
2. 不做一把梭的 try-except——按故障半径圈定异常边界（单轮失败不拖垮循环，
   但网络错误要显式降级成 data_unavailable 而不是静默吞掉）
3. 不引入非必要依赖：requests / bottle / python-okx 之外需先讨论
4. 复杂计算下沉为确定性函数（因子层），LLM 只做解读不碰浮点
5. 涉钱的写操作必须幂等（唯一索引 + INSERT OR IGNORE）
6. 面板要筛选/聚合的字段就是真列，其余进 JSON；禁止在 DB 外并行记账
7. 每个阶段结束时仓库可独立验证：compileall + 全部测试 + replay
8. 测试离线优先：StubClient / ReplayClient / fixture，唯一碰网络的命令是 check-env
9. 中文注释/日志面向操作者；标识符用英文
10. 密钥永远走 SecretStr，repr/traceback 不出明文
