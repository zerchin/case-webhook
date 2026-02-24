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

