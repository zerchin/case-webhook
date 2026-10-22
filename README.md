# case-webhook v0.6

通过 PagerDuty 获取 Case，按队列分配 Support，并通过 Slack 工作流发送通知。
v0.6 将英文管理 UI、管理接口和 PagerDuty Webhook 集成到同一个 Flask 服务、同一个端口，不再需要独立的 `case-webhook-ui` 容器。

## 启动

先创建 MySQL 数据库（支持现有 MySQL 5.7 数据库）：

```sql
CREATE DATABASE IF NOT EXISTS case_system
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

构建并运行：

```bash
docker build -t case-webhook:v0.6 .

docker run -d \
  --name case-webhook-v6 \
  -p 80:5000 \
  -e MYSQL_HOST=192.168.2.68 \
  -e MYSQL_PORT=3306 \
  -e MYSQL_USER=case_webhook \
  -e MYSQL_PASSWORD='<database password>' \
  -e MYSQL_DB=case_system \
  -e ADMIN_PASSWORD='<admin password>' \
  -e PORT=5000 \
  case-webhook:v0.6
```

访问 `http://<server>/`，使用 `admin` 和 `ADMIN_PASSWORD` 登录。直接运行 `app.py` 时，缺少 `ADMIN_PASSWORD` 会拒绝启动。v0.6 只有管理员账号。登录有效期为 8 小时，服务重启后需要重新登录。

首次访问会自动创建缺失的 `support_list`、`cases`、`app_settings` 表。示例中的 `case_webhook` 账号需事先创建，并具备该数据库的读写和建表权限；数据库本身需要提前创建。已有 Support、Case 数据会保留。

自动建表不会改造已有同名表。如果旧库的 `app_settings` 是 `name/value` 或 `key/value` 结构，需要先备份并处理结构冲突，不能直接覆盖旧表。

在 **Settings** 中配置并保存：

| 设置 | 说明 |
| --- | --- |
| Webhook path | 固定前缀 `/webhooks/pd/v3/`，只需填写自定义后缀，或点击按钮生成随机后缀 |
| Slack webhook URL | Slack 工作流 Webhook，工作流接收名为 `message` 的输入 |
| PAGERDUTY_WEBHOOK_SECRET | 填写原始 Signing Secret，使用 Base64 编码保存在数据库中 |

这三项仅保存在 MySQL 中，保存后立即生效，容器重建后仍保留。Slack URL 和 Signing Secret 不会通过读取接口返回，页面显示是否已配置；输入框留空表示保留原值，勾选清除才会删除保存的配置。

Webhook 后缀允许 1–239 位字母、数字、连字符或下划线；生成按钮默认生成 64 位随机十六进制后缀。后缀留空会关闭 Webhook。不带固定前缀的旧路径已停用，数据库地址不会自动改写，需要在 Settings 保存新后缀，并同步更新 PagerDuty 中的完整 URL。

## 从 v0.5 升级

1. 备份现有数据库，确认历史时间使用的时区（见下文）。
2. 启动新容器前，使用 `docker stop <旧后端容器名>` 停止旧后端容器，释放示例中使用的 80 端口。
3. 使用 v0.6 镜像连接原数据库，设置数据库 env 和 `ADMIN_PASSWORD`。
4. 登录 Settings，填写 Webhook 后缀、Slack URL 和原始 Signing Secret 后保存。旧 env 不会自动迁移。
5. 如果原路径已是 `/webhooks/pd/v3/<suffix>`，填写相同后缀即可继续使用；其他旧路径已停用，需要保存新后缀并更新 PagerDuty Subscription 中的 URL。
6. 停用独立 UI 容器。UI 重新分配直接调用认证后的内部逻辑，不再发外部 supplement 请求。

`SUPPLEMENT_TRUSTED_IPS` 已删除。**所有外部 supplement 请求都被拒绝，包括有有效 PagerDuty 签名的请求**。旧 UI 不能再通过 supplement 修改优先级，应改用 v0.6 集成 UI。

从 v0.3/v0.4 升级时，先完成旧的表结构升级（已有字段不要重复添加）：

```sql
ALTER TABLE support_list
  MODIFY updated_at DATETIME DEFAULT NULL COMMENT '更新时间';
ALTER TABLE support_list
  ADD COLUMN priority INT DEFAULT 100 COMMENT '优先级' AFTER status;
```



## 时间与时区

v0.6 的数据库连接统一使用 UTC，新写入的 `created_at`、`updated_at` 均为 UTC 时间。UI 按浏览器时区显示时间，手动设置 Updated At 时也按浏览器时区输入，保存前转换为 UTC。

历史 DATETIME 记录会按 UTC 解释，不会自动转换。旧数据已是 UTC 时无需处理；如果使用其他时区，需要先备份，再按实际时区另行转换。

## Case 分配逻辑

1. 已存在的 Case 不会重复分配或重复发送通知。
2. 在线 Support 中有 Priority `>100` 的人员时，选择其中 Updated At 最早的人，Priority `-1`，时间不变。
3. 否则从 Updated At 最早的在线 Support 开始；Priority `<100` 的人员本轮跳过，Priority `+1` 并更新时间，再继续轮询。
4. 分配给正常 Priority 的人员后更新其 Updated At。相同时间以 ID 升序确定顺序。
5. Case 写入、轮询时间和优先级修改在同一事务中完成；队列操作使用数据库行锁，避免同时处理重复请求时重复推进队列。

Slack 通知在数据库事务提交后发送，失败最多尝试 15 次，退避等待上限为 30 秒。最终失败会返回 `207 partial_success`，UI 明确显示 Case 已保存、通知失败。此时应检查 Slack 和 Settings，不要重复执行重新分配，否则会再次调整 Priority。



## PagerDuty 签名与请求

在 PagerDuty 创建 V3 Webhook Subscription，保存只显示一次的 Signing Secret 到 Settings。普通请求必须通过原始 Body 的 HMAC-SHA256 校验：

- 未设置 Signing Secret：`503`。
- 缺少 `X-PagerDuty-Signature`：`400`。
- 签名无效：`403`。
- 支持 Secret 轮换期间 Header 中多个逗号分隔的 `v1` 签名。
- 发往当前已配置 Webhook URL 的 supplement 请求始终返回 `403`；未配置或不符合固定前缀的路径返回 `404`。

请求示例：

```json
{"event":{"data":{"title":"Case 01590054 - Medium - Customer Company"}}}
```

模拟请求（以下变量仅在发送请求的终端使用，URL 必须与 Settings 中保存的完整 URL 一致，Secret 使用原始值）：

```bash
WEBHOOK_URL='http://<server>/webhooks/pd/v3/<saved-suffix>'
SIGNING_SECRET='<PagerDuty Webhook Signing Secret>'
payload='{"event":{"data":{"title":"Case 01602520 - Medium - Customer‘s Company"}}}'
signature="$(printf '%s' "$payload" | openssl dgst -sha256 -hmac "$SIGNING_SECRET" -hex | awk '{print $NF}')"

curl -X POST \
  -H 'Content-Type: application/json' \
  -H "X-PagerDuty-Signature: v1=${signature}" \
  --data-binary "$payload" \
  "$WEBHOOK_URL"
```

Slack 工作流收到的 `message`：

```text
Case 01590054 - Medium - Customer Company
Owner: Tom Li <@U02345ABCD1234>
```

## 本地开发与测试

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 设置与 Docker 示例相同的数据库 env 和 ADMIN_PASSWORD 后运行
.venv/bin/python app.py

.venv/bin/python -m unittest discover -s tests -v
node --check static/app.js
```

自动化测试使用隔离 SQLite 适配器验证 SQL 操作、事务回滚、分配结果和 Flask 接口；适配器移除 MySQL 的 `FOR UPDATE`，不能证明真实 MySQL 的锁与并发行为。真实 MySQL 验证方法见 `tests/test_mysql.py`，必须使用独立测试数据库。

健康检查：`GET /health` 返回版本 `0.6.0` 和进程健康状态，不检测数据库连接。
