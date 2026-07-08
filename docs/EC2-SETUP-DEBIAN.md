# EC2 侧手动步骤（Debian）

现有 EC2 是 Debian，**默认没有 SSM Agent，也没有 CloudWatch Agent**。本方案的 Reconciler
Lambda 会自动给「无 instance profile」的实例挂上 `ec2-cwagent-profile`（含 SSM +
CloudWatchAgentServerPolicy）。CPU 与 StatusCheck 告警无需 agent 即可生效；但**内存 /
磁盘 / 网络 ethtool** 指标需要在实例内安装并运行 CloudWatch Agent。

> 下面命令中的 `<region>` 请替换成实例所在区域，例如 `ap-northeast-1`。

## 1. 确认 IAM Role 已挂

- Reconciler 会**自动**给没有 profile 的实例挂 `ec2-cwagent-profile`。
- 若实例**原本已有其他 profile**，本方案默认**不会强制替换**（避免破坏你的配置）。此时你需要
  手动确保该 role 含以下两条 managed policy：
  - `AmazonSSMManagedInstanceCore`
  - `CloudWatchAgentServerPolicy`
- 或部署时用 `-c forceAttachProfile=true` 让 Reconciler 强制替换成 cwagent profile。

验证已挂载：

```bash
aws ec2 describe-iam-instance-profile-associations \
  --filters Name=instance-id,Values=<instance-id> --region <region>
```

> 挂/换 instance profile 后，实例内可能需要 ~1-2 分钟凭证才生效。

## 2. 安装 SSM Agent（Debian amd64）

```bash
wget https://s3.<region>.amazonaws.com/amazon-ssm-<region>/latest/debian_amd64/amazon-ssm-agent.deb
sudo dpkg -i amazon-ssm-agent.deb
sudo systemctl enable amazon-ssm-agent
sudo systemctl start amazon-ssm-agent
sudo systemctl status amazon-ssm-agent --no-pager
```

装好后到 SSM 控制台 → Fleet Manager 应能看到该实例为 Managed。

## 3. 安装 CloudWatch Agent（Debian amd64）

```bash
wget https://amazoncloudwatch-agent.s3.amazonaws.com/debian/amd64/latest/amazon-cloudwatch-agent.deb
sudo dpkg -i -E ./amazon-cloudwatch-agent.deb
```

> ethtool 插件需要较新版的 CloudWatch Agent（建议用上面的 `latest` 包）。

## 4. 放置 CloudWatch Agent 配置

把本仓库的 `cwagent/config.json` 拷到实例：

```bash
sudo cp config.json /opt/aws/amazon-cloudwatch-agent/etc/config.json
```

该配置采集：

- `mem_used_percent`（内存）
- `disk_used_percent`（根分区 `/`）
- ethtool allowance-exceeded 计数器（`bw_in / bw_out / pps / conntrack / linklocal`）
- `namespace = CWAgent`，`append_dimensions.InstanceId = ${aws:InstanceId}`
- **`run_as_user = cwagent`**

### 维度对齐（关键：告警只按聚合后的维度匹配）

告警是按**精确维度集合**匹配 CloudWatch 数据点的——只有当存在维度**完全等于**告警所设维度的数据点时，
告警才能进入 OK/ALARM，否则一直停在 INSUFFICIENT_DATA。CW Agent 各指标原始维度与告警所需维度不一致，
因此本配置用 `aggregation_dimensions: [["InstanceId"], ["InstanceId","path"]]` 把它们**额外**汇总（rollup）
成告警需要的纯维度数据点：

- **ethtool**：Agent 原始上报维度是 `[InstanceId, interface=eth0]`。`["InstanceId"]` 这条聚合会把
  `interface` 汇掉，**额外**发布一份纯 `[InstanceId]` 数据点——这正是 ethtool 告警（只带 `InstanceId`）
  所匹配的那份。
- **disk**：Agent 原始维度是 `[InstanceId, device, fstype, path]`。`["InstanceId","path"]` 这条聚合把
  `device`、`fstype` 汇掉，发布纯 `[InstanceId, path]` 数据点——磁盘告警只需 `InstanceId + path=/` 即可命中。
- **mem**：本就是单维度 `[InstanceId]`，告警只带 `InstanceId`，天然对齐（`["InstanceId"]` 聚合对它是恒等）。

> 一句话：`aggregation_dimensions` 里必须保留 `["InstanceId"]` 和 `["InstanceId","path"]`，否则 ethtool/disk
> 告警会因为找不到匹配维度的数据点而一直 INSUFFICIENT_DATA。

## 5. 启动 Agent

```bash
sudo /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
  -a fetch-config -m ec2 -c file:/opt/aws/amazon-cloudwatch-agent/etc/config.json -s
```

查看状态：

```bash
sudo /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a status
sudo tail -f /opt/aws/amazon-cloudwatch-agent/logs/amazon-cloudwatch-agent.log
```

## 6. 验证指标已上报

```bash
aws cloudwatch list-metrics --namespace CWAgent \
  --dimensions Name=InstanceId,Value=<instance-id> --region <region>
```

应能看到 `mem_used_percent`、`disk_used_percent` 以及 `ethtool_*_allowance_exceeded`。
上报后，Reconciler 创建的 `ec2mon-<instanceId>-mem/disk/...` 告警会从 INSUFFICIENT_DATA
转为 OK。

## 7. （可选）用 SSM 批量下发，避免逐台登录

一旦 SSM Agent 就绪，可用 **SSM Run Command / State Manager** 批量安装与配置，思路：

1. 先手动或用 user-data 装好 SSM Agent（或用支持 SSM 的 AMI）。
2. 用托管文档 `AWS-ConfigureAWSPackage` 安装 `AmazonCloudWatchAgent`：
   ```bash
   aws ssm send-command \
     --document-name "AWS-ConfigureAWSPackage" \
     --targets Key=tag:Monitor,Values=true \
     --parameters '{"action":["Install"],"name":["AmazonCloudWatchAgent"]}' \
     --region <region>
   ```
3. 把 `cwagent/config.json` 存进 SSM Parameter Store（如 `/cwagent/config`），再用托管文档
   `AmazonCloudWatch-ManageAgent` 下发并启动：
   ```bash
   aws ssm send-command \
     --document-name "AmazonCloudWatch-ManageAgent" \
     --targets Key=tag:Monitor,Values=true \
     --parameters '{"action":["configure"],"mode":["ec2"],"optionalConfigurationSource":["ssm"],"optionalConfigurationLocation":["/cwagent/config"],"optionalRestart":["yes"]}' \
     --region <region>
   ```
4. 用 **State Manager** 把上述关联设为定期执行，保证新实例自动合规。

## 重要经验

- **CloudWatch Agent 用 `cwagent` 用户跑，不要用 root**（沿用老经验，配置里已固定
  `run_as_user: cwagent`）。用 root 跑过一次后再切回 `cwagent` 可能因文件属主残留报权限错，
  遇到时清理 `/opt/aws/amazon-cloudwatch-agent/logs` 与 `.../var` 下的属主即可。
- CloudWatch Agent 上报的 namespace 必须与告警一致（本方案统一为 `CWAgent`）。
- 磁盘告警维度带 `path=/`，需 agent 配置里 `disk.resources` 含 `/`（已配置）；`device`/`fstype` 由
  `aggregation_dimensions: ["InstanceId","path"]` 汇掉，所以告警只需 `InstanceId + path=/` 即可命中。
