#!/usr/bin/env bash
#
# deploy-cwagent.sh — 一键在「所有带监控 tag 的 EC2」上批量部署 CloudWatch Agent。
#
# 做三件事（全程走 SSM，无需 SSH 登录任何实例）：
#   1. 把仓库里的 cwagent/config.json 存进 SSM Parameter Store（默认 /cwagent/config）
#   2. 用托管文档 AWS-ConfigureAWSPackage 批量安装 AmazonCloudWatchAgent
#   3. 用托管文档 AmazonCloudWatch-ManageAgent 下发该配置并启动 agent
#
# 前置条件：
#   - 目标实例已是 SSM Managed（SSM Agent 运行中 + 挂了含 AmazonSSMManagedInstanceCore 的 role）。
#     本方案的 Reconciler 会自动给「无 profile」实例挂 ec2-cwagent-profile；已装 SSM 的实例
#     reboot 一次即可注册。用 `aws ssm describe-instance-information` 可确认。
#   - 本机已配好 AWS CLI 凭证，且有 ssm:PutParameter / SendCommand 等权限。
#
# 用法：
#   ./scripts/deploy-cwagent.sh                       # 用默认 region + tag Monitor=true
#   REGION=ap-northeast-1 ./scripts/deploy-cwagent.sh
#   TAG_KEY=Env TAG_VALUE=prod ./scripts/deploy-cwagent.sh
#   ./scripts/deploy-cwagent.sh --dry-run             # 只打印将要执行的命令，不实际下发
#
# 环境变量（均可选）：
#   REGION       目标区域（默认取 AWS CLI 解析出的 region）
#   TAG_KEY      监控 tag 键（默认 Monitor）
#   TAG_VALUE    监控 tag 值（默认 true）
#   PARAM_NAME   Parameter Store 参数名（默认 /cwagent/config）
#   CONFIG_FILE  本地 config.json 路径（默认脚本同级 ../cwagent/config.json）
#   INSTALL_WAIT 等待安装命令完成的最大秒数（默认 600）
#   CONFIGURE_WAIT 等待配置命令完成的最大秒数（默认 300）

set -euo pipefail

# ---- 配置（带默认值，可用环境变量覆盖）------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${CONFIG_FILE:-$SCRIPT_DIR/../cwagent/config.json}"
TAG_KEY="${TAG_KEY:-Monitor}"
TAG_VALUE="${TAG_VALUE:-true}"
PARAM_NAME="${PARAM_NAME:-/cwagent/config}"
DRY_RUN=false

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=true ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数：$arg（用 --help 看用法）" >&2; exit 2 ;;
  esac
done

# region：显式 REGION > AWS CLI 默认解析
REGION="${REGION:-$(aws configure get region 2>/dev/null || echo "${AWS_REGION:-${AWS_DEFAULT_REGION:-}}")}"
if [[ -z "$REGION" ]]; then
  echo "✗ 无法确定 region。请设置 REGION=xxx 或配置 AWS CLI 默认 region。" >&2
  exit 1
fi

# ---- 前置检查 --------------------------------------------------------------
command -v aws >/dev/null || { echo "✗ 未找到 aws CLI" >&2; exit 1; }
[[ -f "$CONFIG_FILE" ]] || { echo "✗ 找不到配置文件：$CONFIG_FILE" >&2; exit 1; }

echo "▶ region      = $REGION"
echo "▶ 目标实例 tag = $TAG_KEY=$TAG_VALUE"
echo "▶ 参数名       = $PARAM_NAME"
echo "▶ 配置文件     = $CONFIG_FILE"
echo

run() {
  if $DRY_RUN; then
    echo "[dry-run] $*"
  else
    "$@"
  fi
}

# 等一条 SSM 命令在所有目标上跑完（成功/失败/超时）。
# 用法：wait_command <command-id> [max_wait_seconds]
# 返回 0 表示全部 Success；非 0 表示有实例失败或整体超时。
wait_command() {
  local cmd_id="$1"
  local max_wait="${2:-600}"   # 默认最多等 10 分钟
  local waited=0 interval=10

  if $DRY_RUN || [[ "$cmd_id" == "DRYRUN" || -z "$cmd_id" ]]; then
    echo "  [dry-run] 跳过等待 CommandId=$cmd_id"
    return 0
  fi

  echo "  ⏳ 等待命令 $cmd_id 在各实例上完成（最多 ${max_wait}s）..."
  while (( waited < max_wait )); do
    # 统计各状态数量。SSM 终态：Success / Failed / Cancelled / TimedOut；
    # 进行中：Pending / InProgress / Delayed。
    local statuses
    statuses=$(aws ssm list-command-invocations --region "$REGION" \
      --command-id "$cmd_id" --details \
      --query 'CommandInvocations[].Status' --output text 2>/dev/null || echo "")

    if [[ -z "$statuses" ]]; then
      # 刚下发，invocation 可能还没登记，稍等
      sleep "$interval"; (( waited += interval )); continue
    fi

    local pending=0 done_ok=0 bad=0 total=0
    for s in $statuses; do
      (( total++ ))
      case "$s" in
        Success) (( done_ok++ )) ;;
        Pending|InProgress|Delayed) (( pending++ )) ;;
        *) (( bad++ )) ;;   # Failed / Cancelled / TimedOut
      esac
    done

    if (( pending == 0 )); then
      if (( bad == 0 )); then
        echo "  ✓ 命令完成：$done_ok/$total 成功"
        return 0
      else
        echo "  ⚠ 命令完成但有失败：成功 $done_ok / 失败 $bad / 共 $total"
        echo "     查失败详情：aws ssm list-command-invocations --region $REGION --command-id $cmd_id --details --query 'CommandInvocations[?Status!=\`Success\`].{Instance:InstanceId,Status:Status}' --output table"
        return 1
      fi
    fi
    printf '     ... 进行中 %d / 完成 %d / 失败 %d（已等 %ds）\r' "$pending" "$done_ok" "$bad" "$waited"
    sleep "$interval"; (( waited += interval ))
  done

  echo ""
  echo "  ⚠ 等待超时（${max_wait}s）——安装可能仍在进行。可稍后手动重跑第 3 步，或增大 INSTALL_WAIT。"
  return 1
}

# ---- 0. 列出将被操作的 Managed 实例（提示可见性）--------------------------
echo "== 0/3 检查匹配到的 SSM Managed 实例 =="
INSTANCE_IDS=$(aws ec2 describe-instances --region "$REGION" \
  --filters "Name=tag:$TAG_KEY,Values=$TAG_VALUE" \
            "Name=instance-state-name,Values=running" \
  --query 'Reservations[].Instances[].InstanceId' --output text 2>/dev/null || true)

if [[ -z "$INSTANCE_IDS" ]]; then
  echo "⚠ 没找到 running 且带 $TAG_KEY=$TAG_VALUE 的实例。确认 tag 是否打对、region 是否正确。"
else
  echo "  匹配实例：$INSTANCE_IDS"
  # 交叉核对哪些已是 SSM Managed（未 Managed 的下发会跳过）
  MANAGED=$(aws ssm describe-instance-information --region "$REGION" \
    --query 'InstanceInformationList[].InstanceId' --output text 2>/dev/null || true)
  echo "  SSM Managed：${MANAGED:-（无）}"
  echo "  （不在 Managed 列表里的实例本次会被 SSM 跳过；请先确保其 SSM Agent 运行 + role 已挂，必要时 reboot）"
fi
echo

# ---- 1. config.json -> Parameter Store ------------------------------------
echo "== 1/3 上传配置到 SSM Parameter Store：$PARAM_NAME =="
run aws ssm put-parameter --region "$REGION" \
  --name "$PARAM_NAME" \
  --type String \
  --overwrite \
  --value "file://$CONFIG_FILE"
echo "  ✓ 已写入 $PARAM_NAME"
echo

# ---- 2. 批量安装 CloudWatch Agent -----------------------------------------
echo "== 2/3 批量安装 AmazonCloudWatchAgent（AWS-ConfigureAWSPackage）=="
INSTALL_CMD_ID=$(run aws ssm send-command --region "$REGION" \
  --document-name "AWS-ConfigureAWSPackage" \
  --targets "Key=tag:$TAG_KEY,Values=$TAG_VALUE" \
  --parameters '{"action":["Install"],"name":["AmazonCloudWatchAgent"]}' \
  --query 'Command.CommandId' --output text 2>/dev/null || echo "DRYRUN")
echo "  ✓ 安装命令已下发（CommandId: $INSTALL_CMD_ID）"
echo

# 关键：等安装命令真正跑完，再下发配置——否则 agent 还没装好，
# ManageAgent configure 会因找不到 agent 而失败（这是本脚本早期版本的坑）。
wait_command "$INSTALL_CMD_ID" "${INSTALL_WAIT:-600}" || {
  echo "✗ CloudWatch Agent 安装未全部成功。已中止配置下发，避免在未装好的实例上失败。"
  echo "  修好/等好后可只重跑配置步骤，或直接重跑本脚本（安装对已装实例是幂等的）。"
  exit 1
}
echo

# ---- 3. 下发配置并启动 -----------------------------------------------------
echo "== 3/3 下发配置并启动 agent（AmazonCloudWatch-ManageAgent）=="
CONFIGURE_CMD_ID=$(run aws ssm send-command --region "$REGION" \
  --document-name "AmazonCloudWatch-ManageAgent" \
  --targets "Key=tag:$TAG_KEY,Values=$TAG_VALUE" \
  --parameters "{\"action\":[\"configure\"],\"mode\":[\"ec2\"],\"optionalConfigurationSource\":[\"ssm\"],\"optionalConfigurationLocation\":[\"$PARAM_NAME\"],\"optionalRestart\":[\"yes\"]}" \
  --query 'Command.CommandId' --output text 2>/dev/null || echo "DRYRUN")
echo "  ✓ 配置命令已下发（CommandId: $CONFIGURE_CMD_ID）"
echo
wait_command "$CONFIGURE_CMD_ID" "${CONFIGURE_WAIT:-300}" || \
  echo "  （配置命令有实例未成功，用上面提示的命令查详情）"
echo

# ---- 收尾提示 --------------------------------------------------------------
cat <<EOF
─────────────────────────────────────────────
✓ 批量部署命令已全部下发。SSM 会异步在各实例上执行。

查看执行结果：
  aws ssm list-command-invocations --region $REGION \\
    --command-id $CONFIGURE_CMD_ID --details \\
    --query 'CommandInvocations[].{Instance:InstanceId,Status:Status}' --output table

几分钟后验证指标上报（换成任一实例 id）：
  aws cloudwatch list-metrics --namespace CWAgent \\
    --dimensions Name=InstanceId,Value=<instance-id> --region $REGION

指标上报后，Reconciler 建的 ec2mon-<id>-mem/disk/... 告警会从
INSUFFICIENT_DATA 转为 OK。
─────────────────────────────────────────────
EOF
