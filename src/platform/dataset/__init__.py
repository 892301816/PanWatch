"""行情数据集层:多源日K provider + 优先级路由 + 本地落库。

与 ``src/platform/marketdata`` 的区别:那一层是「单标的 × 多源」的 Engine;
这一层是「市场级批量」的数据集同步,专门服务全市场日K落库与选股。
"""
