# case-webhook
通过 PagerDuty 获取 Case 信息，按顺序分配 Support 人员，并通过 Slack 进行通知。

## 使用教程
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
  `updated_at` DATETIME DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


## 插入数据
INSERT INTO support_list (name,id, status, created_at, updated_at)
VALUES ('Tom Li', 'U02345ABCD1234', 'online', NOW(), NOW());

## 更新数据
UPDATE support_list
SET status = 'offline'
WHERE name = 'Tom Li';
```

3. 创建 Webhook 服务器
```bash
docker run -d --name webhook-receiver \
  -p 80:5000 \
  -e SLACK_WEBHOOK_URL=<WEBHOOK_URL> \
  -e DB_HOST=<DB_HOST> \
  -e DB_USER=<DB_USER> \
  -e DB_PASSWORD=<DB_PSWD> \
  -e DB_DATABASE="case_system" \
  -e PORT=5000 \
  -e LOG_LEVEL=DEBUG \
  -e DEFAULT_OWNER_NAME="fallback_user" \
  -e DEFAULT_OWNER_ID="U00000000" \
  zerchin/case-webhook:v0.2
```
替换其中数据库配置和 Slack Webhook 地址。

4. PagerDuty 接收到的数据格式如下：
```json
{
  "event": {
    "data": {
      "title": "Case 01590054 - Medium - Customer's Company",
    }
  }
}
```

5. Slack 创建工作流
使用 Webhook，并设置自定义参数 message，当触发时转发到 channel 即可。
当触发 Webhook 之后，发送的数据格式如下：
```
Case 01590054 - Medium - Customer's Company
Owner: Tom Li <@U02345ABCD1234>
```
这里使用`<@user_id>`的方式实现艾特的功能，但是工作流貌似无法渲染出来，实际在 channel 里还是看到 ID，不过不影响艾特的功能。

6. 请假设置
通过在对应时间点，设置 support 的 status 为 online 或者 offline。
```bash
## 首先安装 at 工具
 apt update && apt install -y at

## 例如请假 2025/08/11 - 2025/08/15
## 则在 08/11 这天设置为 offline
echo "docker exec -it mysql-env mysql -uroot -p<MYSQL_PASSWORD> -e \"use case_system;UPDATE support_list SET status = 'offline' WHERE name = 'Tom Li';\" "|  at 00:00 2025-08-11


## 在 08/16 这天 设置为online
echo "docker exec -it mysql-env mysql -uroot -p<MYSQL_PASSWORD> -e \"use case_system;UPDATE support_list SET status = 'online' WHERE name = 'Tom Li';\" "|  at 00:00 2025-08-16

```
