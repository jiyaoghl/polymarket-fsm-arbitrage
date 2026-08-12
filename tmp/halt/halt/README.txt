# halt 目录用于存放熔断 lock 文件
# 此目录由 RiskGuard 自动管理，请勿手动删除内部文件
# 若需人工解除红牌熔断，删除 HALT.lock 文件后重启系统

# 文件说明：
# HALT.lock   — 红牌熔断（需人工删除）
# ORANGE.lock — 橙牌限流（重启时自动清除）
# YELLOW.lock — 黄牌警告（重启时自动清除）
