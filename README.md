# ec2-metric-alarm-feishu

对**打了指定 tag 的 EC2**做 CloudWatch 资源指标监控（CPU / 内存 / 磁盘 / 网络 / 状态检查），
告警经 SNS 推送到**飞书**群机器人。基础设施用 **AWS CDK v2 (TypeScript)**，Lambda 用
**Python 3.12**。

核心设计：**告警在运行时由 Reconciler Lambda 用 boto3 动态创建/删除**（不是 CDK 资源）。
因此实例删除时对应告警自动清理，而 `cdk destroy` 只拆基础设施、保留其它实例仍在用的一切。

## 架构

```
                          ┌─────────────────────────────────────────────┐
                          │                CDK Stack (常驻)              │
                          │                                              │
  EC2 state change ──┐    │   EventBridge Rule (state-change)  ─┐        │
  EC2 tag change  ───┼───▶│   EventBridge Rule (tag-change)    ─┼─▶ Reconciler Lambda (Py3.12)
                     │    │                                     │        │   │  boto3:
  cdk deploy ────────┘    │   Custom Resource (full-reconcile) ─┘        │   │   - describe EC2 (tag filter)
                          │                                              │   │   - associate IAM profile*
                          │   IAM Instance Profile                       │   │   - put/delete_metric_alarm
                          │     ec2-cwagent-profile                      │   │
                          │     (SSM + CloudWatchAgentServerPolicy)      │   ▼
                          │                                              │  ec2mon-<id>-* 告警 (运行时管理)
                          │   SNS Topic  ec2-metric-alerts               │   │
                          │        │                                     │   │ AlarmActions/OKActions
                          │        ▼                                     │◀──┘
                          │   Feishu Forwarder Lambda ──▶ 飞书群机器人    │
                          └─────────────────────────────────────────────┘
  * 仅对「无 profile」的实例自动挂载；已有 profile 默认跳过（除非 forceAttachProfile）。
```

告警项与默认阈值：

| 指标 | 来源 | 默认阈值 | 需要 CW Agent |
|------|------|---------|:---:|
| CPUUtilization | AWS/EC2 | > 80%，3×5min | ❌ |
| StatusCheckFailed | AWS/EC2 | ≥ 1，2×1min（missing=breaching） | ❌ |
| mem_used_percent | CWAgent | > 85%，3×5min | ✅ |
| disk_used_percent (`path=/`) | CWAgent | > 85%，3×5min | ✅ |
| ethtool `*_allowance_exceeded` | CWAgent | > 0，1×5min（**默认开**） | ✅ |
| net_bytes_sent/recv | CWAgent | 可选，默认关 | ✅ |

> **维度对齐**：告警按**精确维度集合**匹配数据点。CW Agent 的 ethtool 原始维度是
> `[InstanceId, interface]`、disk 是 `[InstanceId, device, fstype, path]`，而告警只用
> `[InstanceId]`（ethtool/mem）或 `[InstanceId, path=/]`（disk）。`cwagent/config.json` 用
> `aggregation_dimensions: [["InstanceId"], ["InstanceId","path"]]` 额外把 ethtool/mem/disk
> 汇总成这些**纯维度**数据点，告警才能命中——否则会一直 INSUFFICIENT_DATA。mem 本就是单维度
> `[InstanceId]`，disk 的 `device`/`fstype` 被聚合汇掉后只剩 `[InstanceId, path]`。详见
> [docs/EC2-SETUP-DEBIAN.md](docs/EC2-SETUP-DEBIAN.md#维度对齐关键告警只按聚合后的维度匹配)。

## 部署步骤

### 1. 安装依赖 & 构建

```bash
npm install
npm run build          # tsc，应无错误
```

### 2. 配置飞书 webhook

复制 `.env.example` 为 `.env` 并填写：

```bash
cp .env.example .env
# 编辑 .env：
#   FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxxx
#   FEISHU_WEBHOOK_SECRET=（开启签名校验才填）
```

也可用 CDK context 覆盖（优先级高于 .env）：`-c feishuWebhookUrl=... -c feishuWebhookSecret=...`

### 3. 部署

```bash
npx cdk bootstrap        # 每个账号/区域首次
npx cdk deploy --require-approval never
```

> 需要 Docker 运行（CDK 打包 Python Lambda）。

部署完成后：
- 存量带 tag 的 EC2 会由 Custom Resource 触发的全量 reconcile 自动创建 `ec2mon-*` 告警；
- 之后新开/打 tag/删除的实例由 EventBridge 事件驱动增量处理。

### 4. EC2 侧安装 SSM + CloudWatch Agent

内存/磁盘/网络指标需要在实例内装 agent，见 **[docs/EC2-SETUP-DEBIAN.md](docs/EC2-SETUP-DEBIAN.md)**
（覆盖现有 Debian 的 x86_64 与 arm64 两种架构）。

### 5. 私有子网 / 无 NAT？先建 VPC 接口端点

若实例在**私有子网且没有 NAT 网关**，agent 无法直接访问 AWS API，需为 SSM 与 CloudWatch
创建一组 **VPC 接口端点（PrivateLink）**，否则 SSM 不 Managed、CWAgent 指标上报失败、告警一直
INSUFFICIENT_DATA。有 NAT 或在公有子网则无需操作。所需端点与配置见
**[docs/PRIVATE-SUBNET-VPC-ENDPOINTS.md](docs/PRIVATE-SUBNET-VPC-ENDPOINTS.md)**：

- `com.amazonaws.<region>.ssm` / `ssmmessages` / `ec2messages`（SSM Agent）
- `com.amazonaws.<region>.monitoring`（CloudWatch 指标 `PutMetricData`）
- `com.amazonaws.<region>.logs`（可选，仅推日志时）

> 每个端点启用 Private DNS、挂放行 443 的安全组、关联私有子网即可。本方案**默认不用 CDK
> 创建**这些端点（有成本），按需手动补。

## 可配置参数（CDK context）

| context key | 默认 | 说明 |
|-------------|------|------|
| `feishuWebhookUrl` | （必填） | 飞书机器人 webhook |
| `feishuWebhookSecret` | `''` | 签名密钥 |
| `monitorTagKey` | `Monitor` | 监控 tag 键 |
| `monitorTagValue` | `true` | 监控 tag 值 |
| `cpuThreshold` | `80` | CPU 阈值(%) |
| `memThreshold` | `85` | 内存阈值(%) |
| `diskThreshold` | `85` | 磁盘阈值(%) |
| `enableNetExceedAlarm` | `true` | ethtool 网络超限告警（**默认开**） |
| `enableNetworkTrafficAlarm` | `false` | net_bytes 流量告警 |
| `netTrafficThreshold` | （字节，可选） | 流量告警阈值 |
| `forceAttachProfile` | `false` | 是否强制替换已有 instance profile |
| `cwagentNamespace` | `CWAgent` | CW Agent namespace |
| `alarmPrefix` | `ec2mon` | 告警名前缀 |

示例：

```bash
npx cdk deploy -c cpuThreshold=90 -c memThreshold=90 \
  -c monitorTagKey=Env -c monitorTagValue=prod -c enableNetExceedAlarm=false
```

## 测试

手动触发告警状态（无需真实越阈），验证飞书收卡：

```bash
# 触发红卡（ALARM）
aws cloudwatch set-alarm-state --alarm-name ec2mon-i-0abc123-cpu \
  --state-value ALARM --state-reason "manual test" --region <region>

# 触发绿卡（OK）
aws cloudwatch set-alarm-state --alarm-name ec2mon-i-0abc123-cpu \
  --state-value OK --state-reason "manual recover" --region <region>
```

手动全量 reconcile（也可用于排障）：

```bash
aws lambda invoke --function-name <ReconcilerFunctionName> \
  --payload '{"mode":"full-reconcile"}' --cli-binary-format raw-in-base64-out \
  /tmp/out.json --region <region> && cat /tmp/out.json
```

## 验收对照

- `cdk deploy` 建出 SNS + Feishu Lambda + IAM profile + Reconciler + EventBridge + Custom Resource。
- 存量带 tag EC2 部署后自动出现 `ec2mon-*` 告警。
- 新开带 tag EC2 → 数分钟内自动加告警（若原无 profile 还会挂 role）。
- terminate EC2 → 该实例 `ec2mon-*` 告警自动删除，SNS/Lambda/IAM 保留。
- `set-alarm-state ALARM/OK` → 飞书收到红/绿卡。

## 常见坑

- **treatMissingData**：资源指标用 `missing`（agent 没上报/实例 stopped 时不误报）；
  StatusCheck 用 `breaching`（真挂了要报）。
- **profile 不强制替换**：默认只给「无 profile」的实例挂 `ec2-cwagent-profile`；已有 profile
  的实例只打 warning。要替换用 `-c forceAttachProfile=true`，或手动给原 role 补 SSM +
  CloudWatchAgentServerPolicy。
- **namespace 必须一致**：agent 配置和告警都用 `CWAgent`，改了一个要一起改（`-c cwagentNamespace=`）。
- **飞书签名**：机器人开了「签名校验」就必须配 `FEISHU_WEBHOOK_SECRET`，否则消息被拒。
- **磁盘维度**：磁盘告警带 `path=/` 维度，agent 的 `disk.resources` 要含 `/`；`device`/`fstype`
  由 `aggregation_dimensions: ["InstanceId","path"]` 汇掉，告警只需 `InstanceId + path=/` 就能命中。
- **ethtool 维度**：ethtool 原始带 `interface=eth0` 维度，靠 `aggregation_dimensions: ["InstanceId"]`
  汇总出纯 `[InstanceId]` 数据点，ethtool 告警才匹配得上；删掉这条聚合会导致 ethtool 告警永远 INSUFFICIENT_DATA。
- **cwagent 用户**：CloudWatch Agent 用 `cwagent` 用户跑，别用 root（见 EC2 文档）。
- **cdk destroy**：只拆基础设施；运行时创建的 `ec2mon-*` 告警**不会**被 destroy 删除（设计如此）。
  如需清理，手动 `aws cloudwatch delete-alarms` 或 terminate 对应实例让 Reconciler 清。

## 目录结构

```
├── bin/app.ts                       # CDK 入口（读 context + .env）
├── lib/ec2-metric-alarm-stack.ts    # 唯一 Stack
├── lambda/
│   ├── feishu_forwarder/handler.py  # SNS -> 飞书卡片（已有）
│   └── reconciler/handler.py        # 运行时告警/profile 管理
├── cwagent/config.json              # CloudWatch Agent 配置
├── docs/
│   ├── SPEC.md                      # 需求规格
│   ├── EC2-SETUP-DEBIAN.md          # EC2 侧安装步骤（amd64 + arm64）
│   └── PRIVATE-SUBNET-VPC-ENDPOINTS.md  # 私有子网无 NAT 时的 VPC 端点
├── .env.example
├── cdk.json  tsconfig.json  package.json
```
