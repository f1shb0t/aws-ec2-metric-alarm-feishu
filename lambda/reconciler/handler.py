"""
Reconciler Lambda — manages CloudWatch alarms and the CloudWatch-Agent IAM
instance profile for EC2 instances tagged for monitoring.

Alarms are created/deleted at RUNTIME via boto3 (NOT CDK), so the alarm
lifecycle is bound to the instance lifecycle while the SNS/Lambda/IAM
infrastructure stays put across `cdk destroy`.

Dispatch (see handler()):
  * CDK Provider custom-resource onEvent      -> full-reconcile (return dict)
  * event {"mode": "full-reconcile"}         -> full-reconcile
  * EventBridge "EC2 Instance State-change"  -> single-instance
  * EventBridge "Tag Change on Resource"     -> single-instance

Alarm naming: ec2mon-<instanceId>-<metricKey>  (prefix overridable via ALARM_PREFIX)
Cleanup:      delete all alarms with prefix ec2mon-<instanceId>-
"""
import json
import os

import boto3
from botocore.exceptions import ClientError

# --------------------------------------------------------------------------
# Config from environment
# --------------------------------------------------------------------------
MONITOR_TAG_KEY = os.environ.get("MONITOR_TAG_KEY", "Monitor")
MONITOR_TAG_VALUE = os.environ.get("MONITOR_TAG_VALUE", "true")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
CWAGENT_INSTANCE_PROFILE_ARN = os.environ.get("CWAGENT_INSTANCE_PROFILE_ARN", "")
CWAGENT_INSTANCE_PROFILE_NAME = os.environ.get("CWAGENT_INSTANCE_PROFILE_NAME", "")

CPU_THRESHOLD = float(os.environ.get("CPU_THRESHOLD", "80"))
MEM_THRESHOLD = float(os.environ.get("MEM_THRESHOLD", "85"))
DISK_THRESHOLD = float(os.environ.get("DISK_THRESHOLD", "85"))

ENABLE_NET_EXCEED_ALARM = os.environ.get("ENABLE_NET_EXCEED_ALARM", "true").lower() == "true"
ENABLE_NET_TRAFFIC_ALARM = os.environ.get("ENABLE_NET_TRAFFIC_ALARM", "false").lower() == "true"
NET_TRAFFIC_THRESHOLD_RAW = os.environ.get("NET_TRAFFIC_THRESHOLD", "").strip()
FORCE_ATTACH_PROFILE = os.environ.get("FORCE_ATTACH_PROFILE", "false").lower() == "true"

ALARM_PREFIX = os.environ.get("ALARM_PREFIX", "ec2mon")
CWAGENT_NAMESPACE = os.environ.get("CWAGENT_NAMESPACE", "CWAgent")

ec2 = boto3.client("ec2")
cw = boto3.client("cloudwatch")

# ethtool allowance-exceeded counters (Sum over period, > 0 means throttled).
ETHTOOL_METRICS = [
    ("netbwin", "ethtool_bw_in_allowance_exceeded", "入带宽超限丢包"),
    ("netbwout", "ethtool_bw_out_allowance_exceeded", "出带宽超限丢包"),
    ("netpps", "ethtool_pps_allowance_exceeded", "PPS 超限丢包"),
    ("netconntrack", "ethtool_conntrack_allowance_exceeded", "连接跟踪表超限丢包"),
    ("netlinklocal", "ethtool_linklocal_allowance_exceeded", "本地代理请求超限"),
]


# ==========================================================================
# Instance tag / state helpers
# ==========================================================================
def _instance_matches_tag(instance: dict) -> bool:
    for tag in instance.get("Tags", []) or []:
        if tag.get("Key") == MONITOR_TAG_KEY and tag.get("Value") == MONITOR_TAG_VALUE:
            return True
    return False


def _describe_instance(instance_id: str):
    try:
        resp = ec2.describe_instances(InstanceIds=[instance_id])
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("InvalidInstanceID.NotFound", "InvalidInstanceID.Malformed"):
            return None
        raise
    for res in resp.get("Reservations", []):
        for inst in res.get("Instances", []):
            return inst
    return None


def _list_monitored_instances():
    """describe_instances filtered by tag + state running/pending."""
    instances = []
    paginator = ec2.get_paginator("describe_instances")
    for page in paginator.paginate(
        Filters=[
            {"Name": f"tag:{MONITOR_TAG_KEY}", "Values": [MONITOR_TAG_VALUE]},
            {"Name": "instance-state-name", "Values": ["running", "pending"]},
        ]
    ):
        for res in page.get("Reservations", []):
            for inst in res.get("Instances", []):
                instances.append(inst)
    return instances


# ==========================================================================
# IAM instance profile attachment
# ==========================================================================
def _ensure_instance_profile(instance_id: str):
    """
    Attach the cwagent instance profile.

    Strategy (per spec): only attach when the instance has NO profile. If it
    already has one, log a warning and skip — unless FORCE_ATTACH_PROFILE, in
    which case replace the existing association.
    """
    try:
        assoc = ec2.describe_iam_instance_profile_associations(
            Filters=[{"Name": "instance-id", "Values": [instance_id]}]
        )
    except ClientError as e:
        print(f"[WARN] describe associations failed for {instance_id}: {e}")
        return

    associations = [
        a
        for a in assoc.get("IamInstanceProfileAssociations", [])
        if a.get("State") in ("associating", "associated")
    ]

    if not associations:
        try:
            ec2.associate_iam_instance_profile(
                IamInstanceProfile={
                    "Arn": CWAGENT_INSTANCE_PROFILE_ARN,
                    "Name": CWAGENT_INSTANCE_PROFILE_NAME,
                },
                InstanceId=instance_id,
            )
            print(f"[INFO] Associated cwagent profile to {instance_id} (had none).")
        except ClientError as e:
            print(f"[WARN] associate profile failed for {instance_id}: {e}")
        return

    # Already has a profile.
    current = associations[0]
    current_arn = current.get("IamInstanceProfile", {}).get("Arn", "")
    if current_arn == CWAGENT_INSTANCE_PROFILE_ARN:
        print(f"[INFO] {instance_id} already has cwagent profile; nothing to do.")
        return

    if not FORCE_ATTACH_PROFILE:
        print(
            f"[WARN] {instance_id} already has a non-cwagent profile ({current_arn}); "
            f"skipping (set FORCE_ATTACH_PROFILE=true to replace). Ensure it has "
            f"SSM + CloudWatchAgentServerPolicy manually."
        )
        return

    try:
        ec2.replace_iam_instance_profile_association(
            IamInstanceProfile={
                "Arn": CWAGENT_INSTANCE_PROFILE_ARN,
                "Name": CWAGENT_INSTANCE_PROFILE_NAME,
            },
            AssociationId=current["AssociationId"],
        )
        print(f"[INFO] Replaced profile on {instance_id} with cwagent profile (forced).")
    except ClientError as e:
        print(f"[WARN] replace profile failed for {instance_id}: {e}")


# ==========================================================================
# Alarm creation / deletion
# ==========================================================================
def _alarm_name(instance_id: str, metric_key: str) -> str:
    return f"{ALARM_PREFIX}-{instance_id}-{metric_key}"


def _put_alarm(
    instance_id: str,
    metric_key: str,
    *,
    namespace: str,
    metric_name: str,
    statistic: str,
    threshold: float,
    comparison: str,
    period: int,
    eval_periods: int,
    treat_missing: str,
    description: str,
    extra_dimensions=None,
):
    dimensions = [{"Name": "InstanceId", "Value": instance_id}]
    if extra_dimensions:
        dimensions.extend(extra_dimensions)

    cw.put_metric_alarm(
        AlarmName=_alarm_name(instance_id, metric_key),
        AlarmDescription=description,
        Namespace=namespace,
        MetricName=metric_name,
        Dimensions=dimensions,
        Statistic=statistic,
        Period=period,
        EvaluationPeriods=eval_periods,
        Threshold=threshold,
        ComparisonOperator=comparison,
        TreatMissingData=treat_missing,
        ActionsEnabled=True,
        AlarmActions=[SNS_TOPIC_ARN],
        OKActions=[SNS_TOPIC_ARN],
    )
    print(f"[INFO] put alarm {_alarm_name(instance_id, metric_key)}")


def _create_alarms(instance_id: str):
    """Create/update the full alarm set for one instance (idempotent)."""
    # CPU (AWS/EC2, no agent).
    _put_alarm(
        instance_id,
        "cpu",
        namespace="AWS/EC2",
        metric_name="CPUUtilization",
        statistic="Average",
        threshold=CPU_THRESHOLD,
        comparison="GreaterThanThreshold",
        period=300,
        eval_periods=3,
        treat_missing="missing",
        description=f"CPU > {CPU_THRESHOLD}% (3x5min) on {instance_id}",
    )

    # StatusCheckFailed (AWS/EC2, no agent). Breaching on missing data.
    _put_alarm(
        instance_id,
        "statuscheck",
        namespace="AWS/EC2",
        metric_name="StatusCheckFailed",
        statistic="Maximum",
        threshold=1,
        comparison="GreaterThanOrEqualToThreshold",
        period=60,
        eval_periods=2,
        treat_missing="breaching",
        description=f"StatusCheckFailed >= 1 (2x1min) on {instance_id}",
    )

    # Memory (CWAgent).
    _put_alarm(
        instance_id,
        "mem",
        namespace=CWAGENT_NAMESPACE,
        metric_name="mem_used_percent",
        statistic="Average",
        threshold=MEM_THRESHOLD,
        comparison="GreaterThanThreshold",
        period=300,
        eval_periods=3,
        treat_missing="missing",
        description=f"Memory > {MEM_THRESHOLD}% (3x5min) on {instance_id}",
    )

    # Disk root filesystem (CWAgent).
    _put_alarm(
        instance_id,
        "disk",
        namespace=CWAGENT_NAMESPACE,
        metric_name="disk_used_percent",
        statistic="Average",
        threshold=DISK_THRESHOLD,
        comparison="GreaterThanThreshold",
        period=300,
        eval_periods=3,
        treat_missing="missing",
        description=f"Disk(/) > {DISK_THRESHOLD}% (3x5min) on {instance_id}",
        extra_dimensions=[{"Name": "path", "Value": "/"}],
    )

    # Network exceed (ethtool) — default ON.
    if ENABLE_NET_EXCEED_ALARM:
        for metric_key, metric_name, human in ETHTOOL_METRICS:
            _put_alarm(
                instance_id,
                metric_key,
                namespace=CWAGENT_NAMESPACE,
                metric_name=metric_name,
                statistic="Sum",
                threshold=0,
                comparison="GreaterThanThreshold",
                period=300,
                eval_periods=1,
                treat_missing="missing",
                description=f"{human} ({metric_name} > 0) on {instance_id}",
            )

    # Network traffic (net_bytes_sent/recv) — optional, default OFF.
    if ENABLE_NET_TRAFFIC_ALARM and NET_TRAFFIC_THRESHOLD_RAW:
        try:
            net_threshold = float(NET_TRAFFIC_THRESHOLD_RAW)
        except ValueError:
            net_threshold = None
        if net_threshold is not None:
            for metric_key, metric_name in (
                ("nettrafficout", "net_bytes_sent"),
                ("nettrafficin", "net_bytes_recv"),
            ):
                _put_alarm(
                    instance_id,
                    metric_key,
                    namespace=CWAGENT_NAMESPACE,
                    metric_name=metric_name,
                    statistic="Average",
                    threshold=net_threshold,
                    comparison="GreaterThanThreshold",
                    period=300,
                    eval_periods=3,
                    treat_missing="missing",
                    description=f"{metric_name} > {net_threshold} bytes on {instance_id}",
                )


def _delete_instance_alarms(instance_id: str):
    """Delete all alarms with the ec2mon-<instanceId>- prefix."""
    prefix = f"{ALARM_PREFIX}-{instance_id}-"
    names = []
    paginator = cw.get_paginator("describe_alarms")
    for page in paginator.paginate(AlarmNamePrefix=prefix):
        for alarm in page.get("MetricAlarms", []):
            names.append(alarm["AlarmName"])
    if names:
        # delete_alarms accepts up to 100 names per call.
        for i in range(0, len(names), 100):
            cw.delete_alarms(AlarmNames=names[i : i + 100])
        print(f"[INFO] deleted {len(names)} alarms for {instance_id}: {names}")
    else:
        print(f"[INFO] no alarms to delete for {instance_id} (prefix {prefix})")


# ==========================================================================
# Orphan cleanup
# ==========================================================================
def _cleanup_orphans(valid_instance_ids: set):
    """
    List all ec2mon-* alarms, extract instance ids, and delete alarms for
    instances that are no longer monitored (untagged or gone).
    """
    orphan_instance_ids = set()
    paginator = cw.get_paginator("describe_alarms")
    for page in paginator.paginate(AlarmNamePrefix=f"{ALARM_PREFIX}-"):
        for alarm in page.get("MetricAlarms", []):
            name = alarm["AlarmName"]
            iid = _extract_instance_id(name)
            if iid and iid not in valid_instance_ids:
                orphan_instance_ids.add(iid)
    for iid in orphan_instance_ids:
        print(f"[INFO] cleaning orphan alarms for {iid}")
        _delete_instance_alarms(iid)


def _extract_instance_id(alarm_name: str):
    """ec2mon-i-0abc123-cpu -> i-0abc123"""
    prefix = f"{ALARM_PREFIX}-"
    if not alarm_name.startswith(prefix):
        return None
    rest = alarm_name[len(prefix) :]
    if not rest.startswith("i-"):
        return None
    # instance id is 'i-' followed by hex chars, then '-<metricKey>'
    parts = rest.split("-")
    # parts[0] == 'i', parts[1] == '<hex>' ; join first two
    if len(parts) < 2:
        return None
    return f"{parts[0]}-{parts[1]}"


# ==========================================================================
# Reconcile modes
# ==========================================================================
def _full_reconcile():
    instances = _list_monitored_instances()
    valid_ids = set()
    for inst in instances:
        iid = inst["InstanceId"]
        valid_ids.add(iid)
        print(f"[INFO] reconciling instance {iid}")
        _ensure_instance_profile(iid)
        _create_alarms(iid)
    _cleanup_orphans(valid_ids)
    return {"reconciled": sorted(valid_ids), "count": len(valid_ids)}


def _single_instance(instance_id: str, state: str = ""):
    """Handle one instance for state-change / tag-change events."""
    if state in ("terminated", "shutting-down"):
        _delete_instance_alarms(instance_id)
        return {"instance": instance_id, "action": "deleted", "state": state}

    inst = _describe_instance(instance_id)
    if inst is None:
        # Gone — clean up its alarms.
        _delete_instance_alarms(instance_id)
        return {"instance": instance_id, "action": "deleted (not found)"}

    matches = _instance_matches_tag(inst)
    inst_state = inst.get("State", {}).get("Name", "")

    if not matches:
        # No longer tagged for monitoring -> remove alarms (leave profile).
        _delete_instance_alarms(instance_id)
        return {"instance": instance_id, "action": "deleted (untagged)"}

    if inst_state in ("terminated", "shutting-down"):
        _delete_instance_alarms(instance_id)
        return {"instance": instance_id, "action": "deleted", "state": inst_state}

    # running / stopped / pending and tagged -> ensure profile + alarms.
    # (stopped keeps alarms; treatMissingData=missing avoids false alerts.)
    _ensure_instance_profile(instance_id)
    _create_alarms(instance_id)
    return {"instance": instance_id, "action": "created/updated", "state": inst_state}


# ==========================================================================
# Entry point
# ==========================================================================
def handler(event, context):
    print(f"[INFO] event: {json.dumps(event, default=str)[:2000]}")

    # ---- CDK Provider custom-resource onEvent ----
    # The Provider framework forwards the full CFN event (including ResponseURL)
    # but handles the CFN response ITSELF; onEvent must RETURN a dict (and raise
    # on failure), NOT PUT to ResponseURL. Detect it by RequestType present.
    if isinstance(event, dict) and "RequestType" in event:
        request_type = event["RequestType"]
        physical_id = event.get("PhysicalResourceId", "ec2-metric-alarm-full-reconcile")
        if request_type in ("Create", "Update"):
            result = _full_reconcile()
            return {"PhysicalResourceId": physical_id, "Data": {"result": json.dumps(result)}}
        # Delete — leave alarms in place (infra teardown keeps alarms per spec).
        print("[INFO] custom resource Delete: leaving runtime alarms intact.")
        return {"PhysicalResourceId": physical_id}

    # ---- Manual / scheduled full reconcile ----
    if isinstance(event, dict) and event.get("mode") == "full-reconcile":
        return _full_reconcile()

    # ---- EventBridge: EC2 state change ----
    detail_type = event.get("detail-type") if isinstance(event, dict) else None
    if detail_type == "EC2 Instance State-change Notification":
        detail = event.get("detail", {})
        return _single_instance(detail.get("instance-id", ""), detail.get("state", ""))

    # ---- EventBridge: tag change ----
    if detail_type == "Tag Change on Resource":
        detail = event.get("detail", {})
        # resources ARNs like arn:aws:ec2:region:acct:instance/i-xxxx
        resources = event.get("resources", [])
        for arn in resources:
            iid = arn.split("/")[-1]
            if iid.startswith("i-"):
                _single_instance(iid)
        return {"handled": len(resources)}

    print("[WARN] unrecognized event; no-op.")
    return {"status": "ignored"}
