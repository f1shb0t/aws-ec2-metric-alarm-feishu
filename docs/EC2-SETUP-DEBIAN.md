# EC2 侧手动步骤（现有 Debian，x86_64 与 arm64 通用）

本文面向**已经存在**的 Debian 实例（**不依赖 user-data**，逐台手动装即可），同时覆盖
**x86_64（amd64）与 arm64（Graviton）两种架构**。现有 Debian **默认没有 SSM Agent，也没有
CloudWatch Agent**。本方案的 Reconciler Lambda 会自动给「无 instance profile」的实例挂上
`ec2-cwagent-profile`（含 SSM + CloudWatchAgentServerPolicy）。CPU 与 StatusCheck 告警无需
agent 即可生效；但**内存 / 磁盘 / 网络 ethtool** 指标需要在实例内安装并运行 CloudWatch Agent。

> 下面命令中的 `<region>` 请替换成实例所在区域，例如 `ap-northeast-1`。

## 0. 先确认架构（amd64 vs arm64）

下面每一步的下载 URL 都因架构而异，先查清楚：

```bash
dpkg --print-architecture
#  amd64  -> x86_64（Intel/AMD）
#  arm64  -> Graviton（arm64）
```

后续步骤把这个值填进对应的 `<arch>` 位置即可。也可以直接把它存成变量，命令原样复制：

```bash
ARCH=$(dpkg --print-architecture)   # amd64 或 arm64
echo "$ARCH"
```

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

> **重要（踩过的坑）**：给**正在运行**的实例新挂 IAM role 后，实例内已经跑着的 agent
> 不会立刻拿到新凭证。挂/换 profile 后请**重启相关 agent，或直接 reboot 一次**——我们当时
> 就是 reboot 之后 SSM 才注册成功。至少执行：
>
> ```bash
> sudo systemctl restart amazon-ssm-agent
> sudo systemctl restart amazon-cloudwatch-agent   # 如已装
> # 若仍不生效：sudo reboot
> ```

## 2. 安装 SSM Agent（按架构选下载路径）

SSM Agent 的 .deb 下载路径按架构区分：**amd64 用 `debian_amd64`，arm64 用 `debian_arm64`**。

```bash
ARCH=$(dpkg --print-architecture)   # amd64 或 arm64

wget https://s3.<region>.amazonaws.com/amazon-ssm-<region>/latest/debian_${ARCH}/amazon-ssm-agent.deb
sudo dpkg -i amazon-ssm-agent.deb
sudo systemctl enable amazon-ssm-agent
sudo systemctl start amazon-ssm-agent
sudo systemctl status amazon-ssm-agent --no-pager
```

显式写法（不用变量）：

- **amd64**：`.../latest/debian_amd64/amazon-ssm-agent.deb`
- **arm64**：`.../latest/debian_arm64/amazon-ssm-agent.deb`

装好、且 IAM role 已挂并重启 agent 后，到 SSM 控制台 → Fleet Manager 应能看到该实例为
Managed。若迟迟不出现，回到第 1 步 reboot 一次。

## 3. 安装 CloudWatch Agent（按架构选下载路径）

CloudWatch Agent 的 .deb 也按架构区分：**amd64 用 `debian/amd64/latest`，arm64 用
`debian/arm64/latest`**。

```bash
ARCH=$(dpkg --print-architecture)   # amd64 或 arm64

wget https://amazoncloudwatch-agent.s3.amazonaws.com/debian/${ARCH}/latest/amazon-cloudwatch-agent.deb
sudo dpkg -i -E ./amazon-cloudwatch-agent.deb
```

显式写法（不用变量）：

- **amd64**：`https://amazoncloudwatch-agent.s3.amazonaws.com/debian/amd64/latest/amazon-cloudwatch-agent.deb`
- **arm64**：`https://amazoncloudwatch-agent.s3.amazonaws.com/debian/arm64/latest/amazon-cloudwatch-agent.deb`

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
- **`run_as_user = cwagent`**（用 `cwagent` 用户跑，不要 root，见文末「重要经验」）

> **⚠️ 关键坑（我们踩过）：ethtool 的 `interface_include` 必须是 `["*"]`，不能写死
> `["eth0"]`。** Debian（以及使用可预测网卡命名的现代内核）上，主网卡叫 **`ens5`** 而不是
> `eth0`（arm64/Graviton 同样可能是 `ens5`/`enp*`）。若配置里写 `["eth0"]`，ethtool 插件
> 找不到接口，`ethtool_*_allowance_exceeded` 指标**根本不会上报**，对应告警永远停在
> INSUFFICIENT_DATA。仓库里的 `cwagent/config.json` 已改为 `["*"]`，匹配所有接口，跨发行版/
> 架构都稳妥。用 `ip -br link` 可查本机网卡名确认。

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
