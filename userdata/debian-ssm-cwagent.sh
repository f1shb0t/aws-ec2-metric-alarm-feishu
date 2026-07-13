#!/bin/bash
#
# debian-ssm-cwagent.sh — Debian EC2 一次性 userdata：装 SSM Agent + CloudWatch Agent 并配置启动。
#
# 用法：
#   创建 Debian 实例时，把本文件「全文」粘贴到 User data 即可（保留第一行 #!/bin/bash）。
#   支持 amd64 / arm64，自动识别架构和 region。
#
# 前置条件（缺一不可，否则装了也推不上指标）：
#   1. 实例挂 IAM Instance Profile，角色附加：
#        - arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore   (SSM 托管)
#        - arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy     (CWAgent 推指标)
#   2. 实例能出网到 S3（下载 .deb）：公网 IP / NAT，或走 SSM+CW+S3 的 VPC Endpoints。
#
# 指标口径与 aws-ec2-metric-alarm-feishu 仓库的 reconciler 完全一致：
#   namespace = CWAgent
#   mem_used_percent / disk_used_percent(带 path=/) / ethtool 网络超限
#   ★ 不要 rename 指标名，否则 reconciler 按原名匹配不到数据。
#
# 排查：全程日志见 /var/log/user-data.log

set -euxo pipefail
exec > >(tee /var/log/user-data.log) 2>&1

ARCH=$(dpkg --print-architecture)   # amd64 / arm64

# ---- IMDSv2 拿 token & region（Debian 官方 AMI 默认禁 IMDSv1）----
TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 300")
REGION=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
  http://169.254.169.254/latest/meta-data/placement/region)

# ============ 1. SSM Agent ============
cd /tmp
wget -q "https://s3.${REGION}.amazonaws.com/amazon-ssm-${REGION}/latest/debian_${ARCH}/amazon-ssm-agent.deb" -O amazon-ssm-agent.deb
dpkg -i -E amazon-ssm-agent.deb
systemctl enable --now amazon-ssm-agent

# ============ 2. CloudWatch Agent ============
cd /tmp
wget -q "https://amazoncloudwatch-agent.s3.amazonaws.com/debian/${ARCH}/latest/amazon-cloudwatch-agent.deb" -O amazon-cloudwatch-agent.deb
dpkg -i -E amazon-cloudwatch-agent.deb

# ---- 写 CWAgent 配置（与仓库 cwagent/config.json 对齐）----
# 注意：heredoc 用「单引号 <<'EOF'」，shell 不展开，${aws:InstanceId} 原样写入交给 CWAgent 解析。
cat > /opt/aws/amazon-cloudwatch-agent/etc/config.json <<'EOF'
{
  "agent": {
    "metrics_collection_interval": 60,
    "run_as_user": "cwagent"
  },
  "metrics": {
    "namespace": "CWAgent",
    "append_dimensions": {
      "InstanceId": "${aws:InstanceId}"
    },
    "aggregation_dimensions": [["InstanceId"], ["InstanceId", "path"]],
    "metrics_collected": {
      "mem": {
        "measurement": ["mem_used_percent"],
        "metrics_collection_interval": 60
      },
      "disk": {
        "resources": ["/"],
        "measurement": ["disk_used_percent"],
        "ignore_file_system_types": ["sysfs", "devtmpfs", "tmpfs"],
        "metrics_collection_interval": 60
      },
      "ethtool": {
        "interface_include": ["*"],
        "metrics_include": [
          "bw_in_allowance_exceeded",
          "bw_out_allowance_exceeded",
          "pps_allowance_exceeded",
          "conntrack_allowance_exceeded",
          "linklocal_allowance_exceeded"
        ]
      }
    }
  }
}
EOF

# ---- 用本地配置文件启动（不依赖 SSM Parameter Store）----
/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
  -a fetch-config -m ec2 -s \
  -c file:/opt/aws/amazon-cloudwatch-agent/etc/config.json

systemctl enable amazon-cloudwatch-agent

echo "=== userdata done ==="
