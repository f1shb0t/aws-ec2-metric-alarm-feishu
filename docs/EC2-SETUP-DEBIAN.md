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

## 1.5 让实例先具备 SSM（破冰）——鸡生蛋问题的解法

Debian 默认**没有 SSM Agent**，而后面的 SSM 批量下发（第 7 步的 `deploy-cwagent.sh`）又依赖
SSM Agent。所以每台实例都需要**先破冰一次**装上 SSM Agent。破冰之后所有运维（CW Agent、
配置更新、补丁）全走 SSM，**永久免 SSH**。按场景选：

### 场景 A：新建实例 → user-data 一劳永逸（推荐）

新起的 Debian 在 **user-data** 里装 SSM Agent，开机自动完成，永不用 SSH：

```bash
#!/bin/bash
ARCH=$(dpkg --print-architecture)   # amd64 / arm64
TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
REGION=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/placement/region)
cd /tmp
wget -q "https://s3.${REGION}.amazonaws.com/amazon-ssm-${REGION}/latest/debian_${ARCH}/amazon-ssm-agent.deb" -O ssm.deb
dpkg -i ssm.deb
systemctl enable --now amazon-ssm-agent
```

配合启动模板/ASG 里挂好 `ec2-cwagent-profile`（或含 `AmazonSSMManagedInstanceCore` 的 role），
新实例开机即 Managed。

### 场景 B：存量实例（已在跑、无 SSM）→ 破冰脚本并发装

存量实例绕不开**一次**远程执行。仓库提供 **[`scripts/bootstrap-ssm.sh`](../scripts/bootstrap-ssm.sh)**，
只用系统自带的 `ssh` + `xargs` 并发（**不依赖 ansible/pssh/跳板**），读一个 hosts 列表，一次性
给一批实例装好并启动 SSM Agent：

```bash
# hosts.txt：一行一个目标，支持 user@host，# 注释
#   admin@203.0.113.10
#   ubuntu@203.0.113.11
#   admin@10.0.1.5

REGION=ap-northeast-1 ./scripts/bootstrap-ssm.sh hosts.txt
# 先看命中哪些、不实际执行：
./scripts/bootstrap-ssm.sh hosts.txt --dry-run
# 自定义 key / 默认用户 / 并发：
SSH_KEY=~/.ssh/my.pem SSH_USER=admin PARALLEL=20 ./scripts/bootstrap-ssm.sh hosts.txt
```

脚本对每台实例：识别架构 → 从其所在 region 下载对应 .deb → 装并 `enable --now`（已在跑的跳过）。
**前置**：本机能 SSH 到目标（key + 安全组放行 22）、目标已挂含 `AmazonSSMManagedInstanceCore`
的 role、能访问 SSM 的 S3 下载源（公有子网/NAT 或已配 VPC 端点）。

破冰完成后，用下面「验证哪些已 Managed」确认，再进入第 7 步批量装 CloudWatch Agent。

```bash
aws ssm describe-instance-information --region <region> \
  --query 'InstanceInformationList[].{Id:InstanceId,Ping:PingStatus}' --output table
```

> **顺序建议**：先 `bootstrap-ssm.sh`（破冰装 SSM）→ 确认 Managed → 再 `deploy-cwagent.sh`
> （批量装/配 CW Agent）。前者一次性，后者可反复用于配置更新。

---

## 2. 安装 SSM Agent（按架构选下载路径）

> 若已用上面 **1.5 的破冰脚本或 user-data** 装好 SSM，本节可跳过；以下是**单台手动**装法。

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

## 7. （推荐）用一键脚本批量下发，避免逐台登录

仓库提供 **[`scripts/deploy-cwagent.sh`](../scripts/deploy-cwagent.sh)**，一条命令即可在**所有
带监控 tag 的实例**上完成：① 把 `cwagent/config.json` 存进 SSM Parameter Store → ② 批量安装
CloudWatch Agent → ③ 下发配置并启动。全程走 SSM，**无需 SSH 登录任何实例**。

```bash
# 默认 region + tag Monitor=true
./scripts/deploy-cwagent.sh

# 指定 region
REGION=ap-northeast-1 ./scripts/deploy-cwagent.sh

# 自定义监控 tag
TAG_KEY=Env TAG_VALUE=prod ./scripts/deploy-cwagent.sh

# 只打印将执行的命令、不实际下发（先看看命中哪些实例）
./scripts/deploy-cwagent.sh --dry-run
```

脚本会先列出「匹配到的 running 实例」与「已是 SSM Managed 的实例」供你核对，再下发命令，最后
打印查看执行结果 / 验证指标上报的命令。

> **前置条件**：目标实例必须已是 **SSM Managed**（SSM Agent 运行中 + 挂了含
> `AmazonSSMManagedInstanceCore` 的 role）。本方案的 Reconciler 会自动给「无 profile」实例挂
> `ec2-cwagent-profile`；已装 SSM 的实例挂/换 role 后 **reboot 一次**即可注册（见第 1 步的坑）。
> 用 `aws ssm describe-instance-information --region <region>` 可确认哪些已 Managed；**未 Managed
> 的实例会被 SSM 跳过**。

### 手动等价命令（脚本背后做的事，供排障参考）

1. 把配置存进 Parameter Store：
   ```bash
   aws ssm put-parameter --name /cwagent/config --type String --overwrite \
     --value file://cwagent/config.json --region <region>
   ```
2. 用托管文档 `AWS-ConfigureAWSPackage` 安装 agent：
   ```bash
   aws ssm send-command --document-name "AWS-ConfigureAWSPackage" \
     --targets Key=tag:Monitor,Values=true \
     --parameters '{"action":["Install"],"name":["AmazonCloudWatchAgent"]}' \
     --region <region>
   ```
3. 用托管文档 `AmazonCloudWatch-ManageAgent` 下发配置并启动：
   ```bash
   aws ssm send-command --document-name "AmazonCloudWatch-ManageAgent" \
     --targets Key=tag:Monitor,Values=true \
     --parameters '{"action":["configure"],"mode":["ec2"],"optionalConfigurationSource":["ssm"],"optionalConfigurationLocation":["/cwagent/config"],"optionalRestart":["yes"]}' \
     --region <region>
   ```
4. 想让**新实例自动合规**：用 **State Manager** 把上述关联设为定期执行即可。

## 重要经验

- **CloudWatch Agent 用 `cwagent` 用户跑，不要用 root**（沿用老经验，配置里已固定
  `run_as_user: cwagent`）。用 root 跑过一次后再切回 `cwagent` 可能因文件属主残留报权限错，
  遇到时清理 `/opt/aws/amazon-cloudwatch-agent/logs` 与 `.../var` 下的属主即可。
- CloudWatch Agent 上报的 namespace 必须与告警一致（本方案统一为 `CWAgent`）。
- 磁盘告警维度带 `path=/`，需 agent 配置里 `disk.resources` 含 `/`（已配置）；`device`/`fstype` 由
  `aggregation_dimensions: ["InstanceId","path"]` 汇掉，所以告警只需 `InstanceId + path=/` 即可命中。
