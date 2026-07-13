#!/usr/bin/env bash
#
# bootstrap-ssm.sh — 存量实例「SSM 破冰」脚本。
#
# 问题：Debian 默认没有 SSM Agent，而 SSM 批量下发又依赖 SSM Agent（鸡生蛋）。
# 本脚本用系统自带的 ssh 并发登录一批实例，装好并启动 amazon-ssm-agent，
# 建立 SSM 通道。破冰后所有后续运维（CW Agent、配置更新、补丁）都走 SSM，
# 不必再 SSH。—— 只需对每台跑「一次」。
#
# 不依赖 ansible / pssh，只用 ssh + xargs -P 并发。
#
# 前置条件：
#   - 本机能 SSH 到目标实例（有 key、安全组放行 22）。
#   - 目标实例已挂含 AmazonSSMManagedInstanceCore 的 IAM role（否则装了也注册不上）。
#     本方案 Reconciler 会自动给「无 profile」实例挂 ec2-cwagent-profile。
#   - 目标能访问 SSM 的 S3 下载源（公有子网/NAT，或已配 VPC 端点）。
#
# hosts 文件格式：一行一个目标，支持 user@host，# 开头为注释，空行忽略。
#   ubuntu@203.0.113.10
#   admin@203.0.113.11
#   admin@10.0.1.5           # 私有 IP（本机需能路由到）
#
# 用法：
#   ./scripts/bootstrap-ssm.sh hosts.txt
#   REGION=ap-northeast-1 ./scripts/bootstrap-ssm.sh hosts.txt
#   SSH_KEY=~/.ssh/mykey.pem SSH_USER=admin ./scripts/bootstrap-ssm.sh hosts.txt
#   PARALLEL=20 ./scripts/bootstrap-ssm.sh hosts.txt
#   ./scripts/bootstrap-ssm.sh hosts.txt --dry-run
#
# 环境变量（均可选）：
#   REGION     SSM Agent 下载源区域（默认 ap-northeast-1，务必改成你的 region）
#   SSH_KEY    ssh 私钥路径（默认用 ssh 默认 key / agent）
#   SSH_USER   当 hosts 行未写 user@ 时用的默认用户（默认 admin，Debian 官方 AMI 常见）
#   PARALLEL   并发数（默认 10）
#   SSH_OPTS   追加的 ssh 选项

set -euo pipefail

REGION="${REGION:-ap-northeast-1}"
SSH_USER="${SSH_USER:-admin}"
PARALLEL="${PARALLEL:-10}"
DRY_RUN=false

HOSTS_FILE=""
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=true ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "未知参数：$arg（用 --help 看用法）" >&2; exit 2 ;;
    *) HOSTS_FILE="$arg" ;;
  esac
done

[[ -n "$HOSTS_FILE" ]] || { echo "✗ 用法：$0 <hosts 文件> [--dry-run]（--help 看详情）" >&2; exit 2; }
[[ -f "$HOSTS_FILE" ]] || { echo "✗ 找不到 hosts 文件：$HOSTS_FILE" >&2; exit 1; }
command -v ssh >/dev/null || { echo "✗ 未找到 ssh" >&2; exit 1; }

# 组装 ssh 选项
SSH_BASE=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -o BatchMode=yes)
[[ -n "${SSH_KEY:-}" ]] && SSH_BASE+=(-i "$SSH_KEY")
[[ -n "${SSH_OPTS:-}" ]] && read -r -a _extra <<< "$SSH_OPTS" && SSH_BASE+=("${_extra[@]}")

# 在远端执行的安装脚本（自动识别架构 + region）。
# 用远端自己的 IMDS 拿 region，失败则回落到本地传入的 $REGION。
REMOTE_SCRIPT='
set -e
ARCH=$(dpkg --print-architecture)
TOKEN=$(curl -s -m 3 -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 60" || true)
R=$(curl -s -m 3 -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/placement/region || true)
R=${R:-__REGION__}
if systemctl is-active --quiet amazon-ssm-agent 2>/dev/null; then
  echo "already-running"; exit 0
fi
cd /tmp
wget -q "https://s3.${R}.amazonaws.com/amazon-ssm-${R}/latest/debian_${ARCH}/amazon-ssm-agent.deb" -O amazon-ssm-agent.deb
sudo dpkg -i amazon-ssm-agent.deb
sudo systemctl enable --now amazon-ssm-agent
sleep 2
systemctl is-active --quiet amazon-ssm-agent && echo "installed-and-running" || { echo "started-but-inactive"; exit 1; }
'
REMOTE_SCRIPT="${REMOTE_SCRIPT//__REGION__/$REGION}"

echo "▶ region(fallback) = $REGION"
echo "▶ 默认 SSH 用户    = $SSH_USER"
echo "▶ 并发数           = $PARALLEL"
echo "▶ hosts 文件       = $HOSTS_FILE"
echo

# 读取有效 host 行
mapfile -t TARGETS < <(grep -vE '^\s*(#|$)' "$HOSTS_FILE" | awk '{print $1}')
[[ ${#TARGETS[@]} -gt 0 ]] || { echo "✗ hosts 文件里没有有效目标" >&2; exit 1; }
echo "共 ${#TARGETS[@]} 台目标：${TARGETS[*]}"
echo

# 单台处理函数（供 xargs 并发调用）。SSH 选项通过环境变量以字符串传入，函数内拆回数组。
process_one() {
  local raw="$1"
  local target="$raw"
  [[ "$raw" == *"@"* ]] || target="${SSH_USER}@${raw}"

  if [[ "$DRY_RUN" == "true" ]]; then
    echo "[dry-run] ssh ${target} '<install amazon-ssm-agent>'"
    return 0
  fi

  # 从字符串重建 ssh 选项数组
  local -a ssh_opts
  IFS=' ' read -r -a ssh_opts <<< "$SSH_BASE_STR"

  local out last
  if out=$(ssh "${ssh_opts[@]}" "$target" "sudo bash -c '$REMOTE_SCRIPT'" 2>&1); then
    last=$(echo "$out" | tail -1)
    echo "✓ ${target}: ${last}"
  else
    echo "✗ ${target}: 失败 -> $(echo "$out" | tail -2 | tr '\n' ' ')"
  fi
}
export -f process_one
export SSH_USER DRY_RUN REMOTE_SCRIPT
export SSH_BASE_STR="${SSH_BASE[*]}"

echo "== 开始并发破冰（装 amazon-ssm-agent）=="
printf '%s\n' "${TARGETS[@]}" | xargs -P "$PARALLEL" -I{} bash -c 'process_one "$@"' _ {}

echo
cat <<EOF
─────────────────────────────────────────────
✓ 破冰命令执行完毕。装好 SSM Agent 且 IAM role 已挂的实例，稍后会在 SSM 注册为 Managed。

验证哪些已 Managed：
  aws ssm describe-instance-information --region $REGION \\
    --query 'InstanceInformationList[].{Id:InstanceId,Ping:PingStatus,Platform:PlatformName}' --output table

下一步：所有实例 Managed 后，用 scripts/deploy-cwagent.sh 批量装/配 CloudWatch Agent。

提示：给正在运行的实例「新挂/换」IAM role 后，SSM 可能要等凭证刷新；
若 15 分钟后仍未 Managed，reboot 一次该实例即可（见 docs/EC2-SETUP-DEBIAN.md 第 1 步的坑）。
─────────────────────────────────────────────
EOF
