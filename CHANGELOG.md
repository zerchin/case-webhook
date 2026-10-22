## v0.2

### 基础更新

- 实现基础 case 分配功能

## v0.3

### 基础更新

- 架构重构
- 解决同一个 case 重复请求 webhook 导致分配给多个 support 的问题

## v0.4

### 基础更新

- 因网络问题可能发送 slack 消息失败，增大发送 slack 请求次数

### UI 更新

- 上线 UI 

### 镜像版本

| App Name        | Image Version                |
| --------------- | ---------------------------- |
| case-webhook    | zerchin/case-webhook:v0.4    |
| case-webhook-ui | zerchin/case-webhook-ui:v0.3 |



## v0.5

### 基础更新

- 增加优先级（补单）功能，如果有补单 case，可以手动指定 case 给补单的 support（该 support 会跳过下一轮），并让接到补单的 support 重新进入队列接单

### UI 更新

- 优化 UI 布局和按钮
- 增加补单 case 重新分配功能

### 镜像版本

| App Name        | Image Version                |
| --------------- | ---------------------------- |
| case-webhook    | zerchin/case-webhook:v0.5    |
| case-webhook-ui | zerchin/case-webhook-ui:v0.4 |



## v0.5-signature

- 引入 PagerDuty V3 Webhook 的 HMAC-SHA256 签名校验，拒绝未签名或签名无效的普通请求。
- 旧版 supplement 请求增加 `SUPPLEMENT_TRUSTED_IPS` IP/CIDR 白名单校验。

## v0.6

- 集成并重新设计管理 UI 风格，不再需要独立 UI 镜像。
- 支持 UI 配置 Webhook 后缀、Slack URL 和 PagerDuty Secret，不再读取 `WEBHOOK_PATH`、`SLACK_Webhook_URL`、`PAGERDUTY_WEBHOOK_SECRET` 环境变量。
- Webhook Path 固定前缀为 `/webhooks/pd/v3/`，旧的非标准路径停用。
- PagerDuty Secret 以 Base64 编码保存。
- 分配及重新分配使用事务和数据库锁，保留重复 Case 去重逻辑。
- 删除 SUPPLEMENT_TRUSTED_IPS，彻底关闭外部 supplement 请求。

## v0.7

- 扩展 PagerDuty Webhook 处理逻辑，查询 incident 和 log_entry，提取更完整的 Case 信息及链接。
- Slack 工作流格式优化，使用 `message`、`url`、`uid`。
- Case Reassign 功能优化。
- Add Case 功能增强，支持更完整的字段。
- Settings 新增 PagerDuty API Token 配置，Base64 编码存入数据库。
