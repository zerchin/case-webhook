# case-webhook
通过 PagerDuty 获取 Case 信息，按顺序分配 Support 人员，并通过 Slack 进行通知。



# Upgrade

### Upgrade from v0.3/v0.4 to v0.5

如果是从 **v0.3/v0.4** 版本升级到 **v0.5**，需要更新一下数据库：

```sql
## 更新 updated_at 字段去掉 ON UPDATE CURRENT_TIMESTAMP 属性
use case_system
ALTER TABLE support_list 
MODIFY updated_at datetime DEFAULT NULL COMMENT '更新时间';

## 增加 priority 字段
ALTER TABLE support_list
ADD COLUMN priority INT DEFAULT 100 COMMENT '优先级' AFTER status;
```

接着使用新的镜像启动即可：

```bash
docker stop case-webhook-v4

docker run -d \
  -p 80:5000 \
  -e MYSQL_HOST=192.168.2.68 \
  -e MYSQL_USER=root \
  -e MYSQL_PASSWORD=123456 \
  -e MYSQL_DB=case_system \
  -e SLACK_Webhook_URL="https://SLACK_WEBHOOK_URL" \
  -e PORT=5000 \
  --name webhook-receiver-v3 \
  zerchin/case-webhook:v0.5
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
docker run -d \
  -p 80:5000 \
  -e MYSQL_HOST=192.168.2.68 \
  -e MYSQL_USER=root \
  -e MYSQL_PASSWORD=123456 \
  -e MYSQL_DB=case_system \
  -e SLACK_Webhook_URL="https://SLACK_WEBHOOK_URL" \
  -e PORT=5000 \
  --name case-webhook \
  zerchin/case-webhook:v0.5

## 国内拉取地址：docker.1ms.run
```
替换其中数据库配置和 Slack Webhook 地址。

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
curl -X POST -H "Content-Type: application/json"  http://192.168.2.68/5c2df3d1-3371-47bd-a9cf-1983e9adc18b -d '{
  "event": {
    "data": {
      "title": "Case 01602520 - Medium - Orient Overseas Container Line Limited"
    }
  }
}'
```



5. Slack 创建工作流
使用 Webhook，并设置自定义参数 message，当触发时转发到 channel 即可。
当触发 Webhook 之后，发送的数据格式如下：
```
Case 01590054 - Medium - Customer's Company
Owner: Tom Li <@U02345ABCD1234>
```
> 这里使用`<@user_id>`的方式实现艾特的功能，但是工作流貌似无法渲染出来，实际在 channel 里还是看到 ID，不过不影响艾特的功能。



## 功能测试

### 重复请求验证

```
## 插入数据
INSERT INTO cases (id, support_name, support_id, created_at)
VALUES ('01603866', 'Tom Li', 'U02345ABCD1234', NOW());

## 请求
curl -X POST -H "Content-Type: application/json"  http://CASE_WEBHOOK_URL -d '{
  "event": {
    "data": {
      "title": "Case 01603866 - Medium - Customer's Company"
    }
  }
}'
```



## UI

```bash
docker run -d \
  --name case-webhook-ui \
  -p 8080:3000 \
  -e ADMIN_PASSWORD=123456 \
  -e DB_HOST=192.168.2.68 \
  -e DB_USER=root \
  -e DB_PASSWORD=123456 \
  -e DB_DATABASE=case_system \
  -e WEBHOOK_URL="http://192.168.2.68/5c2df3d1-3371-47bd-a9cf-1983e9adc18b" \
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