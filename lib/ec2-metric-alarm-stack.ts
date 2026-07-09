import * as path from 'path';
import { Duration, Stack, StackProps, CustomResource } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import { LambdaSubscription } from 'aws-cdk-lib/aws-sns-subscriptions';
import { Provider } from 'aws-cdk-lib/custom-resources';

export interface MonitorConfig {
  readonly feishuWebhookUrl: string;
  readonly feishuWebhookSecret: string;
  readonly monitorTagKey: string;
  readonly monitorTagValue: string;
  readonly cpuThreshold: string;
  readonly memThreshold: string;
  readonly diskThreshold: string;
  readonly enableNetExceedAlarm: boolean;
  readonly enableNetworkTrafficAlarm: boolean;
  readonly netTrafficThreshold?: string;
  readonly forceAttachProfile: boolean;
  readonly cwagentNamespace: string;
  readonly alarmPrefix: string;
}

export interface Ec2MetricAlarmStackProps extends StackProps {
  readonly monitorConfig: MonitorConfig;
}

export class Ec2MetricAlarmStack extends Stack {
  constructor(scope: Construct, id: string, props: Ec2MetricAlarmStackProps) {
    super(scope, id, props);

    const cfg = props.monitorConfig;

    // ---------------------------------------------------------------------
    // 1. SNS Topic — alarm actions publish here; Feishu Lambda subscribes.
    // ---------------------------------------------------------------------
    const topic = new sns.Topic(this, 'AlertsTopic', {
      topicName: 'ec2-metric-alerts',
      displayName: 'EC2 Metric Alerts',
    });

    // ---------------------------------------------------------------------
    // 2. Feishu forwarder Lambda (existing handler) subscribed to SNS.
    // ---------------------------------------------------------------------
    const feishuFn = new lambda.Function(this, 'FeishuForwarder', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'lambda', 'feishu_forwarder')),
      timeout: Duration.seconds(15),
      memorySize: 128,
      logRetention: logs.RetentionDays.TWO_WEEKS,
      environment: {
        FEISHU_WEBHOOK_URL: cfg.feishuWebhookUrl,
        FEISHU_WEBHOOK_SECRET: cfg.feishuWebhookSecret,
      },
    });
    topic.addSubscription(new LambdaSubscription(feishuFn));

    // The forwarder enriches cards with the EC2 Name tag (SNS alarm payloads only
    // carry the InstanceId dimension), so it needs to describe instances.
    feishuFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['ec2:DescribeInstances'],
        resources: ['*'], // DescribeInstances does not support resource-level scoping
      })
    );

    // ---------------------------------------------------------------------
    // 3. IAM Instance Profile for CloudWatch Agent (SSM + CWAgent).
    // ---------------------------------------------------------------------
    const cwagentRole = new iam.Role(this, 'CwAgentRole', {
      roleName: 'ec2-cwagent-role',
      assumedBy: new iam.ServicePrincipal('ec2.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonSSMManagedInstanceCore'),
        iam.ManagedPolicy.fromAwsManagedPolicyName('CloudWatchAgentServerPolicy'),
      ],
      description: 'Role for EC2 instances running SSM + CloudWatch Agent (managed by ec2-metric-alarm-feishu).',
    });

    const instanceProfile = new iam.CfnInstanceProfile(this, 'CwAgentInstanceProfile', {
      instanceProfileName: 'ec2-cwagent-profile',
      roles: [cwagentRole.roleName],
    });

    // ---------------------------------------------------------------------
    // 4. Reconciler Lambda — runtime alarm + profile management (boto3).
    // ---------------------------------------------------------------------
    const reconcilerFn = new lambda.Function(this, 'Reconciler', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'lambda', 'reconciler')),
      timeout: Duration.minutes(5),
      memorySize: 256,
      logRetention: logs.RetentionDays.TWO_WEEKS,
      environment: {
        MONITOR_TAG_KEY: cfg.monitorTagKey,
        MONITOR_TAG_VALUE: cfg.monitorTagValue,
        SNS_TOPIC_ARN: topic.topicArn,
        CWAGENT_INSTANCE_PROFILE_ARN: instanceProfile.attrArn,
        CWAGENT_INSTANCE_PROFILE_NAME: instanceProfile.instanceProfileName!,
        CPU_THRESHOLD: cfg.cpuThreshold,
        MEM_THRESHOLD: cfg.memThreshold,
        DISK_THRESHOLD: cfg.diskThreshold,
        ENABLE_NET_EXCEED_ALARM: String(cfg.enableNetExceedAlarm),
        ENABLE_NET_TRAFFIC_ALARM: String(cfg.enableNetworkTrafficAlarm),
        NET_TRAFFIC_THRESHOLD: cfg.netTrafficThreshold ?? '',
        FORCE_ATTACH_PROFILE: String(cfg.forceAttachProfile),
        ALARM_PREFIX: cfg.alarmPrefix,
        CWAGENT_NAMESPACE: cfg.cwagentNamespace,
      },
    });

    // Reconciler IAM permissions (per spec section 5).
    reconcilerFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          'ec2:DescribeInstances',
          'ec2:DescribeIamInstanceProfileAssociations',
          'ec2:AssociateIamInstanceProfile',
          'ec2:ReplaceIamInstanceProfileAssociation',
          'ec2:DisassociateIamInstanceProfile',
        ],
        resources: ['*'], // these EC2 describe/associate actions do not support resource-level scoping
      })
    );
    reconcilerFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['cloudwatch:PutMetricAlarm', 'cloudwatch:DeleteAlarms', 'cloudwatch:DescribeAlarms'],
        resources: ['*'],
      })
    );
    // PassRole: allow handing the cwagent role to EC2 instances.
    reconcilerFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['iam:PassRole'],
        resources: [cwagentRole.roleArn],
        conditions: {
          StringEquals: { 'iam:PassedToService': 'ec2.amazonaws.com' },
        },
      })
    );

    // ---------------------------------------------------------------------
    // 5. EventBridge rules -> Reconciler (single-instance mode).
    // ---------------------------------------------------------------------
    // Rule 1: EC2 Instance State-change Notification.
    const stateChangeRule = new events.Rule(this, 'Ec2StateChangeRule', {
      description: 'EC2 instance state change -> reconcile alarms for that instance.',
      eventPattern: {
        source: ['aws.ec2'],
        detailType: ['EC2 Instance State-change Notification'],
        detail: {
          state: ['running', 'terminated', 'stopped', 'shutting-down'],
        },
      },
    });
    stateChangeRule.addTarget(new targets.LambdaFunction(reconcilerFn));

    // Rule 2: Tag change on EC2 instances.
    const tagChangeRule = new events.Rule(this, 'Ec2TagChangeRule', {
      description: 'EC2 instance tag change -> reconcile alarms for that instance.',
      eventPattern: {
        source: ['aws.tag'],
        detailType: ['Tag Change on Resource'],
        detail: {
          service: ['ec2'],
          'resource-type': ['instance'],
        },
      },
    });
    tagChangeRule.addTarget(new targets.LambdaFunction(reconcilerFn));

    // ---------------------------------------------------------------------
    // 6. Custom Resource -> trigger full-reconcile on deploy/update.
    // ---------------------------------------------------------------------
    const provider = new Provider(this, 'FullReconcileProvider', {
      onEventHandler: reconcilerFn,
      logRetention: logs.RetentionDays.ONE_WEEK,
    });

    const fullReconcile = new CustomResource(this, 'FullReconcileOnDeploy', {
      serviceToken: provider.serviceToken,
      properties: {
        // Bump this to force a re-run on every deploy (config changes already do).
        mode: 'full-reconcile',
        monitorTagKey: cfg.monitorTagKey,
        monitorTagValue: cfg.monitorTagValue,
        cpuThreshold: cfg.cpuThreshold,
        memThreshold: cfg.memThreshold,
        diskThreshold: cfg.diskThreshold,
        enableNetExceedAlarm: String(cfg.enableNetExceedAlarm),
        enableNetworkTrafficAlarm: String(cfg.enableNetworkTrafficAlarm),
      },
    });
    // Custom resource must run after IAM profile + topic exist.
    fullReconcile.node.addDependency(instanceProfile);
    fullReconcile.node.addDependency(topic);
  }
}
