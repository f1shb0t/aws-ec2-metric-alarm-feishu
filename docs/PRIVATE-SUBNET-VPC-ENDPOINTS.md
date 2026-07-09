# 私有子网 / VPC 接口端点（无 NAT 场景）

## 什么时候需要看这篇

SSM Agent 与 CloudWatch Agent 都要**主动调用 AWS API**（SSM 注册与会话、`PutMetricData`
上报指标等）。这些调用默认走公网 endpoint。因此：

- 实例在**公有子网**，或在**私有子网但有 NAT 网关**出网 —— **什么都不用做**，agent 能直接
  到达 AWS API，本篇可跳过。
- 实例在**私有子网、且没有 NAT 网关**（完全无出网）—— 你必须在 VPC 里创建一组
  **接口型 VPC 端点（Interface Endpoints / PrivateLink）**，否则 agent 连不上 AWS，
  会表现为：SSM 里实例一直不 Managed、CWAgent 日志里 `PutMetricData` 超时、
  `ec2mon-*` 内存/磁盘/ethtool 告警永远 INSUFFICIENT_DATA。

> **一句话判断**：有 NAT → 不需要端点；无 NAT 的私有子网 → 需要下列端点。

## 需要的 VPC 接口端点

把 `<region>` 换成实例所在区域（如 `ap-northeast-1`）：

| 端点服务名 | 用途 | 是否必需 |
|-----------|------|:---:|
| `com.amazonaws.<region>.ssm` | SSM 核心 API（注册、命令下发） | ✅ |
| `com.amazonaws.<region>.ssmmessages` | Session Manager / 长连接通道 | ✅ |
| `com.amazonaws.<region>.ec2messages` | SSM Agent 收取指令的消息通道 | ✅ |
| `com.amazonaws.<region>.monitoring` | CloudWatch 指标上报（`PutMetricData`） | ✅（内存/磁盘/ethtool 指标必需） |
| `com.amazonaws.<region>.logs` | CloudWatch Logs（若 agent 还推日志） | ⭕ 可选，仅推日志时需要 |

> `monitoring` 就是 CloudWatch **指标**的 endpoint（不是 `cloudwatch`）。本方案的自定义指标
> （`mem_used_percent` / `disk_used_percent` / `ethtool_*`）全靠它上报，私有无 NAT 时不建这个，
> CWAgent 相关告警一定会卡在 INSUFFICIENT_DATA。

## 每个端点怎么配

对上面每一个接口端点：

1. **类型选 Interface**（接口型，走 PrivateLink，不是 Gateway 型）。
2. **启用 Private DNS**（Enable DNS name）——这样 agent 用默认的 AWS API 域名就能自动解析到
   端点私有 IP，**无需改任何 agent 配置**。（需要 VPC 打开 `enableDnsSupport` 与
   `enableDnsHostnames`。）
3. **关联到实例所在的私有子网**（选这些子网，端点会在每个子网放一个 ENI）。
4. **挂一个安全组，放行来自实例的 443/TCP**（HTTPS）。最简单：新建一个 SG，入站规则
   允许「实例所在的 SG / 私有子网 CIDR」到 `443`。

创建示例（SSM 为例，其余端点把 `service-name` 换掉即可）：

```bash
aws ec2 create-vpc-endpoint \
  --vpc-endpoint-type Interface \
  --vpc-id <vpc-id> \
  --service-name com.amazonaws.<region>.ssm \
  --subnet-ids <private-subnet-a> <private-subnet-b> \
  --security-group-ids <endpoint-sg-allowing-443> \
  --private-dns-enabled \
  --region <region>
```

对 `ssmmessages`、`ec2messages`、`monitoring`（以及可选的 `logs`）重复上述命令，只改
`--service-name`。

## 验证

```bash
# 端点应为 available
aws ec2 describe-vpc-endpoints --region <region> \
  --filters Name=vpc-id,Values=<vpc-id> \
  --query 'VpcEndpoints[].{svc:ServiceName,state:State,dns:PrivateDnsEnabled}' --output table

# 实例内：域名应解析到私有 IP（10.x/172.x），而不是公网 IP
nslookup ssm.<region>.amazonaws.com
nslookup monitoring.<region>.amazonaws.com
```

之后 SSM Fleet Manager 里实例应变 Managed，CWAgent 指标开始出现在 `CWAgent` namespace，
`ec2mon-*` 告警从 INSUFFICIENT_DATA 转 OK。

## 为什么本方案默认不用 CDK 建这些端点

接口端点**按小时 + 按流量计费**（每个端点每 AZ 都有固定小时费）。多数环境要么在公有子网、
要么已有 NAT，建了反而白花钱。因此本仓库的 CDK Stack **默认不创建** VPC 端点——是否需要取决于
你的网络拓扑，按上面手动补即可。

> 未来可考虑在 CDK 里加一个 context flag（如 `-c createVpcEndpoints=true` + `-c vpcId=...`
> `-c privateSubnetIds=...`）按需创建这几个端点，但当前**刻意不做默认**以免产生费用。
