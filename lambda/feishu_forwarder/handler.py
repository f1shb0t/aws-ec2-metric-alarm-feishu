"""
CloudWatch Alarm (via SNS) -> Feishu interactive card forwarder.

SNS 消息体中 Records[i].Sns.Message 是 CloudWatch Alarm 的 JSON。
本函数解析后转成飞书自定义机器人的 interactive card 并 POST 出去。

环境变量：
  FEISHU_WEBHOOK_URL     飞书群自定义机器人 webhook（必填）
  FEISHU_WEBHOOK_SECRET  机器人签名校验密钥（开启签名校验时必填；未开启留空）
"""
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.request
import urllib.error

import boto3

FEISHU_WEBHOOK_URL = os.environ.get("FEISHU_WEBHOOK_URL", "").strip()
FEISHU_WEBHOOK_SECRET = os.environ.get("FEISHU_WEBHOOK_SECRET", "").strip()

# boto3 EC2 clients per region, lazily created & reused within the invocation.
_EC2_CLIENTS = {}


def _ec2_client(region: str):
    """Return a cached EC2 client for the given region (None -> Lambda default)."""
    key = region or "__default__"
    client = _EC2_CLIENTS.get(key)
    if client is None:
        client = boto3.client("ec2", region_name=region) if region else boto3.client("ec2")
        _EC2_CLIENTS[key] = client
    return client


def _lookup_instance_name(instance_id: str, region: str, cache: dict):
    """Resolve the EC2 Name tag for an instance id. Caches per-invocation.

    Returns the Name string, or None if lookup fails / no Name tag exists.
    """
    if not instance_id:
        return None
    cache_key = (region or "", instance_id)
    if cache_key in cache:
        return cache[cache_key]

    name = None
    try:
        resp = _ec2_client(region).describe_instances(InstanceIds=[instance_id])
        for reservation in resp.get("Reservations", []):
            for inst in reservation.get("Instances", []):
                for tag in inst.get("Tags", []) or []:
                    if tag.get("Key") == "Name":
                        name = tag.get("Value")
                        break
    except Exception as e:  # noqa — best-effort enrichment, never fatal
        print(f"describe_instances failed for {instance_id} in {region or 'default'}: {e}")

    cache[cache_key] = name
    return name


def _gen_sign(secret: str):
    """飞书机器人签名：timestamp + '\\n' + secret 作为 key，对空串做 HMAC-SHA256。"""
    ts = str(int(time.time()))
    string_to_sign = f"{ts}\n{secret}"
    digest = hmac.new(
        string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
    ).digest()
    return ts, base64.b64encode(digest).decode("utf-8")


def _build_card(alarm: dict, instance_line: str = "") -> dict:
    name = alarm.get("AlarmName", "-")
    desc = alarm.get("AlarmDescription", "") or ""
    state = alarm.get("NewStateValue", "-")        # ALARM / OK / INSUFFICIENT_DATA
    reason = alarm.get("NewStateReason", "-")
    region = alarm.get("Region", "-")
    ts = alarm.get("StateChangeTime", "-")
    account = alarm.get("AWSAccountId", "-")

    trigger = alarm.get("Trigger", {}) or {}
    metric_name = trigger.get("MetricName", "-")
    dims = trigger.get("Dimensions", []) or []
    dim_str = ", ".join(
        f"{d.get('name')}={d.get('value')}" for d in dims
    ) or "-"

    if state == "ALARM":
        color, emoji, title_word = "red", "🔴", "告警"
    elif state == "OK":
        color, emoji, title_word = "green", "✅", "恢复"
    else:
        color, emoji, title_word = "grey", "⚪", "数据不足"

    content = f"**状态**: {state}\n"
    if instance_line:
        content += f"**实例**: {instance_line}\n"
    content += (
        f"**指标**: {metric_name}\n"
        f"**维度**: {dim_str}\n"
        f"**Region**: {region}　**Account**: {account}\n"
        f"**原因**: {reason}\n"
        f"**时间**: {ts}"
    )
    if desc:
        content += f"\n**说明**: {desc}"

    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"{emoji} [{title_word}] {name}",
                },
                "template": color,
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": content}}
            ],
        },
    }
    return card


def _post_feishu(card: dict):
    if not FEISHU_WEBHOOK_URL:
        print("ERROR: FEISHU_WEBHOOK_URL is empty; skip sending.")
        return

    payload = dict(card)
    if FEISHU_WEBHOOK_SECRET:
        ts, sign = _gen_sign(FEISHU_WEBHOOK_SECRET)
        payload["timestamp"] = ts
        payload["sign"] = sign

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        FEISHU_WEBHOOK_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = resp.read().decode("utf-8", "ignore")
            print(f"Feishu resp {resp.status}: {body}")
    except urllib.error.HTTPError as e:
        print(f"Feishu HTTPError {e.code}: {e.read().decode('utf-8','ignore')}")
        raise
    except Exception as e:  # noqa
        print(f"Feishu send failed: {e}")
        raise


def _instance_line(alarm: dict, name_cache: dict) -> str:
    """Build the '<Name> (i-xxxx)' line from the alarm's InstanceId dimension.

    Falls back to just the instance id (or "") when no Name tag is available.
    """
    trigger = alarm.get("Trigger", {}) or {}
    dims = trigger.get("Dimensions", []) or []
    instance_id = None
    for d in dims:
        if d.get("name") == "InstanceId":
            instance_id = d.get("value")
            break
    if not instance_id:
        return ""

    # The alarm "Region" field is a human display name (e.g. "US East (N. Virginia)"),
    # not a region code — parse the code from AlarmArn instead. Fall back to the
    # Lambda default region when the ARN is missing/unparsable.
    region = _region_from_arn(alarm.get("AlarmArn", ""))
    name = _lookup_instance_name(instance_id, region, name_cache)
    return f"{name} ({instance_id})" if name else instance_id


def _region_from_arn(arn: str):
    """arn:aws:cloudwatch:<region>:<acct>:alarm:<name> -> <region>, or None."""
    parts = arn.split(":")
    if len(parts) >= 4 and parts[3]:
        return parts[3]
    return None


def handler(event, context):
    records = event.get("Records", [])
    if not records:
        print("No SNS records in event.")
        return {"statusCode": 200, "body": "no records"}

    # Cache Name-tag lookups across all records in this invocation.
    name_cache = {}

    for rec in records:
        sns = rec.get("Sns", {})
        raw = sns.get("Message", "{}")
        try:
            alarm = json.loads(raw)
        except json.JSONDecodeError:
            # 不是标准 Alarm JSON（比如订阅确认或纯文本），原样转发
            alarm = {
                "AlarmName": sns.get("Subject", "SNS Message"),
                "NewStateValue": "INSUFFICIENT_DATA",
                "NewStateReason": raw,
            }
        instance_line = _instance_line(alarm, name_cache)
        card = _build_card(alarm, instance_line)
        _post_feishu(card)

    return {"statusCode": 200, "body": "ok"}
