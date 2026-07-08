# ec2-metric-alarm-feishu — 需求规格 / 实现指令

> 这是给实现者（coding agent）的完整规格。请严格按此实现。技术栈：**AWS CDK v2 + TypeScript**，Lambda 用 **Python 3.12**。

## 一、目标（用户原话精炼）

对**打了指定 tag 的 EC2**（当前场景：Debian 上跑 TiDB，但只做**资源指标**监控，不管 TiDB 服务本身）做 CloudWatch 监控告警，推送飞书。要求：

1. 只监控 **EC2 资源指标**：CPU、内存、磁盘、网络、状态检查（instance/system status check）。
2. CDK 一次部署，既建「SNS+Lambda 推飞书」这套基础设施，又**自动**给所有带 tag 的现存 EC2：
   - 附加 CloudWatch Agent 所需的 IAM instance profile（含 SSM + CloudWatchAgentServerPolicy）
   - 创建全套资源告警
3. **后续新开的 EC2**，只要带这个 tag，自动补齐告警 + IAM role（事件驱动）。
4. EC2 **被删除**后，该实例对应的告警要**自动删除**；但 SNS+Lambda+IAM 这套基础设施**保留**（别的实例还要用）。

## 二、Tag 约定

- 默认 tag：`Monitor` = `true`
- 可通过 CDK context / 环境变量覆盖 key 和 value：
  - `-c monitorTagKey=Monitor -c monitorTagValue=true`

## 三、告警项与默认阈值（全部可通过 CDK context override）

| 指标 | 命名空间 / 来源 | 维度 | 默认阈值 | 说明 |
|------|----------------|------|---------|------|
| CPUUtilization | AWS/EC2（默认） | InstanceId | > 80%, 3× 5min | 无需 agent |
| StatusCheckFailed | AWS/EC2（默认） | InstanceId | >= 1, 2× 1min | 无需 agent |
| mem_used_percent | CWAgent（自定义） | InstanceId | > 85%, 3× 5min | 需 CW Agent |
| disk_used_percent | CWAgent（自定义） | InstanceId, path=/, device, fstype | > 85%, 3× 5min | 需 CW Agent |
| **net 超限（ethtool）** | CWAgent（ethtool 插件） | InstanceId, interface | **默认开**，阈值 > 0 | 实例网络性能触顶被 AWS 限流，异常计数器，见下 |
| net 流量 in/out | CWAgent（net 插件）net_bytes_sent/recv | InstanceId | 可选，默认关闭（flag 开） | 绝对流量阈值难定，非默认 |

- context 覆盖示例：`-c cpuThreshold=90 -c memThreshold=90 -c diskThreshold=90 -c enableNetworkTrafficAlarm=true`
- **网络超限（ethtool）告警默认开启**，无需 flag；如需关闭用 `-c enableNetExceedAlarm=false`。

### 网络超限（ethtool allowance）指标 —— 默认开启

EC2 实例有网络性能上限（带宽/PPS/连接跟踪/本地代理），CW Agent 的 **ethtool 插件**采集以下「只增计数器」，正常恒为 0，> 0 即被 AWS 限流，是明确异常信号：

| metric（CWAgent ethtool） | 含义 | 告警条件 |
|---------------------------|------|---------|
| `ethtool_bw_in_allowance_exceeded` | 入带宽超限丢包 | > 0（Sum, 1× 5min） |
| `ethtool_bw_out_allowance_exceeded` | 出带宽超限丢包 | > 0 |
| `ethtool_pps_allowance_exceeded` | PPS 超限丢包 | > 0 |
| `ethtool_conntrack_allowance_exceeded` | 连接跟踪表超限丢包 | > 0 |
| `ethtool_linklocal_allowance_exceeded` | 本地代理(DNS/元数据)请求超限 | > 0（可选） |

> 这些是累计计数器，用 `Sum` 统计 + 阈值 `> 0` 判断周期内是否发生限流。treatMissingData=missing。
> 若只想要一个聚合告警，可用 metric math 把几个 exceeded 相加，但分开更易定位。默认分别建 bw_in/bw_out/pps/conntrack 四个。
- 每个 alarm：`treatMissingData` 对资源指标用 `missing`（不误报）；StatusCheck 用 `breaching`。
- alarm 命名规范：`ec2mon-<instanceId>-<metricKey>`，例如 `ec2mon-i-0abc123-cpu`。删除时按 `ec2mon-<instanceId>-` 前缀批量清理。
- 所有 alarm 的 AlarmActions 和 OKActions 都指向基础设施 SNS Topic。

## 四、Stack 设计（单 Stack 即可，逻辑分组）

### 基础设施（常驻，删除 EC2 不受影响）
1. **SNS Topic** `ec2-metric-alerts`
2. **飞书转发 Lambda**（复用 `lambda/feishu_forwarder/handler.py`，已实现，签名校验支持），订阅 SNS。
   - 环境变量：`FEISHU_WEBHOOK_URL`, `FEISHU_WEBHOOK_SECRET`（从 CDK context / .env 注入）
3. **IAM Instance Profile** `ec2-cwagent-profile`
   - Role 挂 managed policy：`AmazonSSMManagedInstanceCore` + `CloudWatchAgentServerPolicy`
4. **Reconciler Lambda**（Python，boto3）：核心逻辑，见第五节。
5. **EventBridge Rules** → 触发 Reconciler Lambda：
   - Rule 1：EC2 Instance State-change Notification，state in [`running`, `terminated`, `stopped`, `shutting-down`]
   - Rule 2：Tag Change on EC2 instance（`aws.tag` / `Tag Change on Resource`），过滤 resource-type = instance
6. **Custom Resource**（CDK `AwsCustomResource` 或 provider Lambda）：部署/更新时触发一次 Reconciler 的「全量扫描」模式，处理存量 EC2。

### 关键：删除 EC2 时保留基础设施
- 告警是 Reconciler Lambda **运行时用 boto3 动态创建/删除**的，**不是 CDK 管理的资源**。这样 EC2 删除 → Lambda 删对应 alarm，基础设施 stack 完全不动。
- 这是本方案的核心设计点：**告警生命周期与实例绑定，走 Lambda 运行时管理；基础设施走 CDK。**

## 五、Reconciler Lambda 逻辑（Python 3.12 + boto3）

入口 `handler(event, context)`，根据 event 来源分派：

### 模式 A：全量 reconcile（Custom Resource 调用，或手动/定时）
event 带 `{"mode": "full-reconcile"}`：
1. `ec2.describe_instances` 过滤 tag `Monitor=true` 且 state 在 running/pending。
2. 对每个实例：
   - 若未挂 instance profile 或挂的不是我们的 → `associate_iam_instance_profile`（或 replace）。
     - 注意：已有 profile 的情况要 `replace_iam_instance_profile_association`，需先查 association。谨慎：**不要覆盖用户已有的、非本方案的 profile**——策略：若实例已有 profile，仅记录 warning 并跳过挂载（打日志），不强制替换（避免破坏用户配置）。或提供 context flag `forceAttachProfile` 决定是否强制。默认**不强制**，只对「无 profile」的实例挂。
   - 创建/更新该实例的全套 alarm（幂等：`put_metric_alarm` 本身幂等）。
3. 清理「已不带 tag 或已不存在」的孤儿 alarm：列出所有 `ec2mon-` 前缀 alarm，反查实例，不符合的删除。

### 模式 B：单实例事件（EventBridge）
- `running` / tag 变化为符合 → 给该实例挂 profile（按上面策略）+ 建 alarm。
- `terminated` / `shutting-down` → 删除该实例 `ec2mon-<id>-*` 全部 alarm。
- tag 被移除（不再符合）→ 删除该实例 alarm（profile 不动，删不了正在用的，且无害）。
- `stopped` → 保留 alarm（实例还在，只是停了），CPU 等会 INSUFFICIENT_DATA，treatMissingData=missing 不误报。

### 环境变量
- `MONITOR_TAG_KEY`, `MONITOR_TAG_VALUE`
- `SNS_TOPIC_ARN`
- `CWAGENT_INSTANCE_PROFILE_ARN`, `CWAGENT_INSTANCE_PROFILE_NAME`
- `CPU_THRESHOLD`, `MEM_THRESHOLD`, `DISK_THRESHOLD`
- `ENABLE_NET_EXCEED_ALARM`（默认 true）, `ENABLE_NET_TRAFFIC_ALARM`（默认 false）, `NET_TRAFFIC_THRESHOLD`
- `FORCE_ATTACH_PROFILE`（默认 false）
- `ALARM_PREFIX`（默认 `ec2mon`）
- `CWAGENT_NAMESPACE`（默认 `CWAgent`）

### Reconciler Lambda IAM 权限
- `ec2:DescribeInstances`, `ec2:DescribeIamInstanceProfileAssociations`, `ec2:AssociateIamInstanceProfile`, `ec2:ReplaceIamInstanceProfileAssociation`, `ec2:DisassociateIamInstanceProfile`
- `cloudwatch:PutMetricAlarm`, `cloudwatch:DeleteAlarms`, `cloudwatch:DescribeAlarms`
- `iam:PassRole`（把 cwagent role 传给 EC2）
- SNS publish 不需要（是 alarm 自己 publish）

## 六、CDK context 参数汇总（bin/app.ts 读取）

| context key | 默认 | 说明 |
|-------------|------|------|
| feishuWebhookUrl | （必填） | 飞书机器人 webhook |
| feishuWebhookSecret | '' | 签名密钥 |
| monitorTagKey | Monitor | |
| monitorTagValue | true | |
| cpuThreshold | 80 | |
| memThreshold | 85 | |
| diskThreshold | 85 | |
| enableNetExceedAlarm | true | 网络超限(ethtool)告警，**默认开** |
| enableNetworkTrafficAlarm | false | net_bytes in/out 流量告警 |
| netTrafficThreshold | （字节，可选） | 流量告警阈值 |
| forceAttachProfile | false | |
| cwagentNamespace | CWAgent | |

支持从 `.env` 读取 feishu 相关（用 dotenv），context 优先级高于 .env。

## 七、EC2 侧手动步骤（写进 docs/EC2-SETUP-DEBIAN.md）

现有 EC2 是 Debian，**没有 SSM agent，也没有 CloudWatch agent**。需给出完整安装步骤：

1. **确认 IAM role 已挂**（本方案 Reconciler 会自动挂 `ec2-cwagent-profile`；若实例原本已有其他 profile 未被自动替换，需手动确保 role 含 SSM + CloudWatchAgentServerPolicy）。
2. **安装 SSM Agent（Debian）**：
   ```bash
   # Debian amd64
   wget https://s3.<region>.amazonaws.com/amazon-ssm-<region>/latest/debian_amd64/amazon-ssm-agent.deb
   sudo dpkg -i amazon-ssm-agent.deb
   sudo systemctl enable amazon-ssm-agent
   sudo systemctl start amazon-ssm-agent
   ```
3. **安装 CloudWatch Agent（Debian）**：
   ```bash
   wget https://amazoncloudwatch-agent.s3.amazonaws.com/debian/amd64/latest/amazon-cloudwatch-agent.deb
   sudo dpkg -i -E ./amazon-cloudwatch-agent.deb
   ```
4. **放 CW Agent 配置**（`cwagent/config.json`，采集 mem_used_percent + disk_used_percent，namespace=CWAgent，append_dimensions InstanceId，run_as_user cwagent）。
5. **启动 agent**：
   ```bash
   sudo /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
     -a fetch-config -m ec2 -c file:/opt/aws/amazon-cloudwatch-agent/etc/config.json -s
   ```
6. 验证 metric 上报到 CWAgent namespace。
7. （可选）用 SSM Run Command / State Manager 批量下发，避免逐台登录——给出思路即可。

**重要经验（写进文档）**：cwagent 用 `cwagent` 用户跑，不要 root（沿用老经验）。

## 八、cwagent 配置文件（cwagent/config.json）

采集：
- mem: `mem_used_percent`
- disk: `disk_used_percent`（resources: `["/"]`，或 `["*"]`）
- **net (ethtool 插件)**：`ethtool` section，采集 allowance exceeded 计数器：
  `bw_in_allowance_exceeded`, `bw_out_allowance_exceeded`, `pps_allowance_exceeded`,
  `conntrack_allowance_exceeded`, `linklocal_allowance_exceeded`
  （ethtool 插件需较新版 cwagent；配置示例见下）
- 可选 diskio / swap / net(net_bytes_sent/recv)
- `append_dimensions`: `{"InstanceId":"${aws:InstanceId}"}`
- `aggregation_dimensions`: `[["InstanceId"]]`
- namespace: `CWAgent`
- `run_as_user`: `cwagent`
- metrics_collection_interval: 60

ethtool 配置片段示例：
```json
"ethtool": {
  "interface_include": ["eth0"],
  "metrics_include": [
    "bw_in_allowance_exceeded",
    "bw_out_allowance_exceeded",
    "pps_allowance_exceeded",
    "conntrack_allowance_exceeded",
    "linklocal_allowance_exceeded"
  ]
}
```

## 九、README.md（面向使用者）

包含：架构图、部署步骤（npm install → 配 .env/context → cdk bootstrap → cdk deploy）、EC2 侧步骤链接、测试方法（`aws cloudwatch set-alarm-state` 手动触发）、常见坑（treatMissingData、profile 不强制替换、metric namespace 一致、飞书签名）。

## 十、验收清单

- [ ] `cdk deploy` 成功，建出 SNS+Lambda+IAM profile+Reconciler+EventBridge+CustomResource
- [ ] 部署后存量带 tag EC2 自动出现 `ec2mon-*` 告警
- [ ] 新开带 tag EC2 → 数分钟内自动加告警 + 挂 role（若原无 profile）
- [ ] terminate EC2 → 该实例 `ec2mon-*` 告警自动消失，SNS/Lambda 保留
- [ ] 手动 set-alarm-state ALARM → 收到飞书红卡；OK → 绿卡
- [ ] 阈值/tag 可通过 context override
- [ ] 代码能 `npm run build` 通过（tsc 无错）
