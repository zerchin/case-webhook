## v0.6 架构

```mermaid
flowchart TD
    Browser[Browser / English UI] -->|Same port| Flask[Flask]
    Flask --> Login[admin session + CSRF]
    Login --> Support[Support management]
    Login --> Cases[Case management]
    Login --> Settings[Settings]
    Settings --> Config[(MySQL app_settings)]
    PD[PagerDuty V3] -->|Configured custom path| Signature[HMAC signature validation]
    Signature --> Auto[Shared automatic assignment]
    Cases -->|Notify enabled| Auto
    Cases -->|Notify disabled| Manual[Manual support selection]
    Cases --> Reassign[Reassign / optional priority adjustment]
    Support --> DB[(MySQL / UTC)]
    Auto --> Transaction[Row lock + transaction]
    Manual --> Transaction
    Reassign --> Transaction
    Transaction --> DB
    Transaction -->|After commit / if enabled| Slack[Slack workflow notification]
    Config --> Signature
    Config --> Slack
    External[External supplement] --> Rejected[403 / disabled]
```

- 页面由 Flask 渲染，静态 CSS/JS 由同一个服务提供；不存在独立前端容器。
- 自动分配、手动分配和重新分配共用 MySQL 事务与队列锁，数据库层按 Support ID 更新。
- Settings 是业务配置唯一来源，数据库连接与管理员密码仍通过 env 提供。
- API 时间使用 UTC ISO 8601，浏览器负责本地化显示和输入转换；历史 DATETIME 不自动改写。
- 通知失败不会回滚已提交的 Case，返回 `207 partial_success`。
