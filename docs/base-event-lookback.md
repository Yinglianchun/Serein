# 自动摘要的归线回看与 Event 候选边界

设置 → 配置 → 自动摘要配置中的 **归线 Track 回看天数** 默认是 3，可保存 1–365 之间的整数。对应设置字段为 `pipeline.track_lookback_days`，通过已有认证设置接口 `PATCH /v1/settings` 修改，例如：

```json
{"pipeline":{"track_lookback_days":3}}
```

旧实例没有保存该字段时使用默认 3 天；保存其他设置不会清除已经设置的天数。API 拒绝布尔值、小数、数字字符串以及范围外的整数。

## Track 时间口径

Track Router 以本批第一条待归线原话的时间为基准，读取此前 N × 24 小时内实际有已保存归线记录的 Track（含边界）；同来源且有明确 runtime／workspace 标记时仍保留隔离。多个聊天会话之间可以连续归线，API 客户端无需提供窗口身份。

Track 以最后一次真实归入原话的时间判断。超过范围只是不送入本批 Router，卡片与已保存归线记录不会删除；再次有新材料时可以建立新 Track。调整天数对新批次和新归线请求生效，已冻结任务不回头重路由。

## Event 候选边界

Event 不按创建时间过期，也不会因为超过三天而归档或退出召回。同一 Track 的所有 active Event leaves 都是 Curator 候选，并完整读取各自绑定原话；`rolling_engineering` 仍按真实建设关系选择全部相关 leaves 做 extend 或 merge。

为避免候选集合无界增长，host 在读取绑定原话前先统计 active leaves。同一 Track 最多允许 8 条；出现第 9 条时，不截断、不按时间挑选，也不让模型在残缺材料上继续创建 Event。该 Track 本轮稳定原话全部 defer，保持未结算，且不调用 Curator 或 Writer；其他未超限 Track 继续正常处理。返回结果中的 `candidate_overflow_deferrals` 记录 Track、实际数量、上限与 Event IDs。

这个边界只限制 active leaf 数量，不改变 Event 正文、生命周期、引用保护、召回资格或原文证据。用户处理误归线、归档不再需要的叶子，或把相关叶子安全合并后，后续新批次可以继续。单条 active Event 自身绑定原话极大时，仍可能触及完整提示词字符上限。

## 测试

`tests/test_pipeline_base_window.py` 与 `tests/test_track_continuation_parity.py` 使用合成数据，覆盖配置校验、跨会话归线、运行环境隔离、1/3/7 天 Track 可见范围、Event 不按年龄消失、8 条候选完整读取、第 9 条读取前 fail closed，以及 overflow Track defer。

```sh
python -m pytest -q tests/test_pipeline_base_window.py tests/test_track_continuation_parity.py
```
