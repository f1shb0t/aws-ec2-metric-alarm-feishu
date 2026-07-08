#!/usr/bin/env node
import * as path from 'path';
import * as dotenv from 'dotenv';
import * as cdk from 'aws-cdk-lib';
import { Ec2MetricAlarmStack } from '../lib/ec2-metric-alarm-stack';

// Load .env (does not override existing process.env). Context wins over .env.
dotenv.config({ path: path.join(__dirname, '..', '.env') });

const app = new cdk.App();

/**
 * Resolve a config value with precedence: CDK context > .env / process.env > default.
 */
function cfg(contextKey: string, envKey: string, def?: string): string | undefined {
  const ctx = app.node.tryGetContext(contextKey);
  if (ctx !== undefined && ctx !== null && `${ctx}`.length > 0) {
    return `${ctx}`;
  }
  const env = process.env[envKey];
  if (env !== undefined && env !== null && env.length > 0) {
    return env;
  }
  return def;
}

function boolCfg(contextKey: string, envKey: string, def: boolean): boolean {
  const raw = cfg(contextKey, envKey, def ? 'true' : 'false');
  return `${raw}`.toLowerCase() === 'true';
}

const feishuWebhookUrl = cfg('feishuWebhookUrl', 'FEISHU_WEBHOOK_URL', '');
const feishuWebhookSecret = cfg('feishuWebhookSecret', 'FEISHU_WEBHOOK_SECRET', '') as string;

if (!feishuWebhookUrl) {
  // Not throwing hard so `cdk synth`/`ls` still works, but warn loudly.
  console.warn(
    '[WARN] feishuWebhookUrl is empty. Set -c feishuWebhookUrl=... or FEISHU_WEBHOOK_URL in .env before deploy.'
  );
}

new Ec2MetricAlarmStack(app, 'Ec2MetricAlarmFeishuStack', {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION,
  },
  description: 'EC2 resource-metric CloudWatch alarms -> SNS -> Feishu, with runtime Reconciler Lambda.',
  monitorConfig: {
    feishuWebhookUrl: feishuWebhookUrl as string,
    feishuWebhookSecret,
    monitorTagKey: cfg('monitorTagKey', 'MONITOR_TAG_KEY', 'Monitor') as string,
    monitorTagValue: cfg('monitorTagValue', 'MONITOR_TAG_VALUE', 'true') as string,
    cpuThreshold: cfg('cpuThreshold', 'CPU_THRESHOLD', '80') as string,
    memThreshold: cfg('memThreshold', 'MEM_THRESHOLD', '85') as string,
    diskThreshold: cfg('diskThreshold', 'DISK_THRESHOLD', '85') as string,
    enableNetExceedAlarm: boolCfg('enableNetExceedAlarm', 'ENABLE_NET_EXCEED_ALARM', true),
    enableNetworkTrafficAlarm: boolCfg('enableNetworkTrafficAlarm', 'ENABLE_NET_TRAFFIC_ALARM', false),
    netTrafficThreshold: cfg('netTrafficThreshold', 'NET_TRAFFIC_THRESHOLD', ''),
    forceAttachProfile: boolCfg('forceAttachProfile', 'FORCE_ATTACH_PROFILE', false),
    cwagentNamespace: cfg('cwagentNamespace', 'CWAGENT_NAMESPACE', 'CWAgent') as string,
    alarmPrefix: cfg('alarmPrefix', 'ALARM_PREFIX', 'ec2mon') as string,
  },
});

app.synth();
