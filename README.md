# case-webhook
通过 PagerDuty 获取 Case 信息，按顺序分配 Support 人员，并通过 Slack 进行通知。



# Upgrade

### Upgrade from v0.3/v0.4 to v0.5

如果是从 **v0.3/v0.4** 版本升级到 **v0.5**，需要更新一下数据库：

```sql
## 更新 updated_at 字段去掉 ON UPDATE CURRENT_TIMESTAMP 属性
USE case_system;
ALTER TABLE support_list 
MODIFY updated_at datetime DEFAULT NULL COMMENT '更新时间';

## 增加 priority 字段
ALTER TABLE support_list
ADD COLUMN priority INT DEFAULT 100 COMMENT '优先级' AFTER status;
```

接着使用新的镜像启动即可：

```bash
docker build -t case-webhook:v0.5-signature .

# 只生成一次，并保存完整的 WEBHOOK_PATH
WEBHOOK_TOKEN="$(openssl rand -hex 32)"
WEBHOOK_PATH="/webhooks/pd/v3/${WEBHOOK_TOKEN}"
echo "$WEBHOOK_PATH"

docker stop case-webhook-v4

docker run -d \
  -p 80:5000 \
  -e MYSQL_HOST=192.168.2.68 \
  -e MYSQL_USER=root \
  -e MYSQL_PASSWORD=123456 \
  -e MYSQL_DB=case_system \
  -e SLACK_Webhook_URL="https://SLACK_WEBHOOK_URL" \
  -e PAGERDUTY_WEBHOOK_SECRET="<PagerDuty Webhook Signing Secret>" \
  -e SUPPLEMENT_TRUSTED_IPS="<case-webhook-ui IP or CIDR>" \
  -e WEBHOOK_PATH="$WEBHOOK_PATH" \
  -e PORT=5000 \
  --name case-webhook-v5 \
  case-webhook:v0.5-signature
```





## Install
1. 安装数据库，这里使用 Docker 一键启动
``` bash
## 实际需要换个复杂的密码
docker run -itd --name case-webhook-mysql -p 3306:3306 -e MYSQL_ROOT_PASSWORD=123456 mysql:5.7
```

2. 创建数据库和表
```mysql
## 创建数据库
CREATE DATABASE IF NOT EXISTS `case_system` 
DEFAULT CHARACTER SET utf8mb4 
DEFAULT COLLATE utf8mb4_unicode_ci;


## 创建表
CREATE TABLE IF NOT EXISTS `support_list` (
  `name` VARCHAR(30) NOT NULL COMMENT '姓名',
  `id` VARCHAR(20) NOT NULL COMMENT 'ID',
  `status` ENUM('online', 'offline') NOT NULL DEFAULT 'online' COMMENT '状态',
  `priority` INT DEFAULT 100 COMMENT '优先级',
  `updated_at` DATETIME DEFAULT NULL COMMENT '更新时间',
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

## 新增 cases 表
CREATE TABLE IF NOT EXISTS `cases` (
  `id` VARCHAR(30) NOT NULL COMMENT 'id',
  `support_name` VARCHAR(30) NOT NULL COMMENT '姓名',
  `support_id` VARCHAR(20) NOT NULL COMMENT 'ID',
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

## 插入数据，参考
INSERT INTO support_list (name,id, status, created_at, updated_at)
VALUES ('Tom Li', 'U02345ABCD1234', 'online', NOW(), NOW());

## 更新数据，参考
UPDATE support_list
SET status = 'offline'
WHERE name = 'Tom Li';
```

3. 创建 Webhook 服务器
```bash
docker build -t case-webhook:v0.5-signature .

# 只生成一次，并保存完整的 WEBHOOK_PATH
WEBHOOK_TOKEN="$(openssl rand -hex 32)"
WEBHOOK_PATH="/webhooks/pd/v3/${WEBHOOK_TOKEN}"
echo "$WEBHOOK_PATH"

docker run -d \
  -p 80:5000 \
  -e MYSQL_HOST=192.168.2.68 \
  -e MYSQL_USER=root \
  -e MYSQL_PASSWORD=123456 \
  -e MYSQL_DB=case_system \
  -e SLACK_Webhook_URL="https://SLACK_WEBHOOK_URL" \
  -e PAGERDUTY_WEBHOOK_SECRET="<PagerDuty Webhook Signing Secret>" \
  -e SUPPLEMENT_TRUSTED_IPS="<case-webhook-ui IP or CIDR>" \
  -e WEBHOOK_PATH="$WEBHOOK_PATH" \
  -e PORT=5000 \
  --name case-webhook \
  case-webhook:v0.5-signature
```
替换其中数据库配置、Slack Webhook 地址、PagerDuty Webhook Signing Secret 和 case-webhook-ui 的连接端 IP/CIDR。

### Webhook 地址

`WEBHOOK_PATH` 是必填环境变量，格式必须为 `/webhooks/pd/v3/<64 位小写十六进制字符>`。使用下列命令生成一次：

```bash
WEBHOOK_TOKEN="$(openssl rand -hex 32)"
WEBHOOK_PATH="/webhooks/pd/v3/${WEBHOOK_TOKEN}"
echo "$WEBHOOK_PATH"
```

将输出的完整路径保存到安全位置，并同时配置到 Webhook 服务器、PagerDuty V3 Webhook URL 和 `case-webhook-ui` 的 `WEBHOOK_URL`。不要在每次启动容器时重新生成。

- 未配置 `WEBHOOK_PATH` 或格式错误时，服务会拒绝启动。
- 原来已暴露的路径和其他未配置路径均返回 `404`。
- PagerDuty 中的完整 URL 形如 `http://<PUBLIC_IP>${WEBHOOK_PATH}`。

### PagerDuty Webhook 签名校验

普通 PagerDuty 请求仅在通过 V3 Webhook 签名校验后才会处理。在 PagerDuty 中创建 V3 Webhook Subscription 时，复制只显示一次的 Signing Secret，并通过 `PAGERDUTY_WEBHOOK_SECRET` 环境变量传入容器。

- 未配置 secret 时，Webhook 返回 `503`，不会在无验证状态下继续处理。
- 缺少 `X-PagerDuty-Signature` 时返回 `400`。
- 签名无效时返回 `403`。
- 签名校验使用原始请求体，支持 secret 轮换时 header 中的多个 `v1` 签名。
- Supplement 请求可以使用 PagerDuty 签名，或来自 `SUPPLEMENT_TRUSTED_IPS` 配置的 IP/CIDR 白名单。
- `SUPPLEMENT_TRUSTED_IPS` 使用逗号分隔，例如 `192.168.2.68,172.17.0.0/16`。请根据日志中的 `remote_ip` 配置实际的 `case-webhook-ui` 连接端 IP，不会信任可伪造的 `X-Forwarded-For`。

### Webhook 请求日志

每个 Webhook 请求都会以单行 JSON 记录连接端 IP、HTTP Method、Path、所有 Header 和完整 Body。`Authorization`、`Cookie`、`X-Api-Key` 和 `X-PagerDuty-Signature` 等敏感 Header 会保留名称但将值记录为 `[REDACTED]`，避免日志可被直接用于请求重放。Body 可能包含业务信息，请限制日志的访问权限和保留时间。

> 注意：该签名机制只能验证来源和内容完整性。Signing Secret 本身不会在请求中传输，但 HTTP 仍不能防止链路上窃听业务数据，或捕获完整请求后重放；条件允许时仍应迁移到 HTTPS。

4. PagerDuty 接收到的数据格式如下：
```json
{
  "event": {
    "data": {
      "title": "Case 01590054 - Medium - Customer‘s Company"
    }
  }
}
```

模拟请求：

```bash
export PAGERDUTY_WEBHOOK_SECRET='<PagerDuty Webhook Signing Secret>'
payload='{"event":{"data":{"title":"Case 01602520 - Medium - Customer‘s Company"}}}'
signature="$(printf '%s' "$payload" | openssl dgst -sha256 -hmac "$PAGERDUTY_WEBHOOK_SECRET" -hex | awk '{print $2}')"

curl -X POST \
  -H 'Content-Type: application/json' \
  -H "X-PagerDuty-Signature: v1=${signature}" \
  --data-binary "$payload" \
  "http://192.168.2.68${WEBHOOK_PATH}"
```



5. Slack 创建工作流
使用 Webhook，并设置自定义参数 message，当触发时转发到 channel 即可。
当触发 Webhook 之后，发送的数据格式如下：
```
Case 01590054 - Medium - Customer‘s Company
Owner: Tom Li <@U02345ABCD1234>
```
> 这里使用`<@user_id>`的方式实现艾特的功能，但是工作流貌似无法渲染出来，实际在 channel 里还是看到 ID，不过不影响艾特的功能。



## 功能测试

### 重复请求验证

```bash
## 插入数据
INSERT INTO cases (id, support_name, support_id, created_at)
VALUES ('01603866', 'Tom Li', 'U02345ABCD1234', NOW());

## 请求
payload='{"event":{"data":{"title":"Case 01603866 - Medium - Customer‘s Company"}}}'
signature="$(printf '%s' "$payload" | openssl dgst -sha256 -hmac "$PAGERDUTY_WEBHOOK_SECRET" -hex | awk '{print $2}')"

curl -X POST \
  -H 'Content-Type: application/json' \
  -H "X-PagerDuty-Signature: v1=${signature}" \
  --data-binary "$payload" \
  "http://CASE_WEBHOOK_IP${WEBHOOK_PATH}"
```



## UI

```bash
docker run -d \
  --name case-webhook-ui-v4 \
  -p 8080:3000 \
  -e ADMIN_PASSWORD=123456 \
  -e DB_HOST=192.168.2.68 \
  -e DB_USER=root \
  -e DB_PASSWORD=123456 \
  -e DB_DATABASE=case_system \
  -e WEBHOOK_URL="http://192.168.2.68${WEBHOOK_PATH}" \
  zerchin/case-webhook-ui:v0.4
```





## 其他功能

### 请假设置

基于 at 工具在对应时间点，设置 support 的 status 实现请假。
```bash
## 首先安装 at 工具
 apt update && apt install -y at

## 例如请假 2025/08/11 - 2025/08/15
## 则在 08/11 这天设置为 offline
echo "mysql -uroot -p<MYSQL_PASSWORD> -e \"use case_system;UPDATE support_list SET status = 'offline' WHERE name = 'Tom Li';\" "|  at 00:00 2025-08-11


## 在 08/16 这天 设置为online
echo "mysql -uroot -p<MYSQL_PASSWORD> -e \"use case_system;UPDATE support_list SET status = 'online' WHERE name = 'Tom Li';\" "|  at 00:00 2025-08-16

```
