"""Remediator Lambda, triggered by the SQS queue of unhealthy targets.

For each incident: skip it if the same target was remediated within the cooldown
(so a flapping service doesn't get redeployed every minute), otherwise force a new
ECS deployment, record the incident in DynamoDB and alert on SNS. Failed records are
returned as batchItemFailures so SQS retries only those, and repeated failures land
in the dead-letter queue.
"""
import json
import os
import time
from datetime import datetime, timezone

import boto3

COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_MINUTES", "10")) * 60


def recently_remediated(table, target, now):
    item = table.get_item(Key={"target": target, "sk": "LATEST"}).get("Item")
    return bool(item) and now - int(item["remediated_at_epoch"]) < COOLDOWN_SECONDS


def remediate(incident, ecs, table, sns, topic_arn, now=None):
    now = int(now or time.time())
    target = incident["target"]
    when = datetime.fromtimestamp(now, timezone.utc).isoformat()

    if recently_remediated(table, target, now):
        action = "suppressed (cooldown)"
    elif incident.get("ecs_service"):
        ecs.update_service(cluster=incident["ecs_cluster"], service=incident["ecs_service"], forceNewDeployment=True)
        action = "forced new ECS deployment"
        table.put_item(Item={"target": target, "sk": "LATEST", "remediated_at_epoch": now})
    else:
        action = "no automatic fix available; alert only"

    table.put_item(Item={"target": target, "sk": f"INCIDENT#{when}", "problems": incident["problems"],
                         "action": action, "checked_at": incident.get("checked_at", when)})
    if not action.startswith("suppressed"):
        sns.publish(TopicArn=topic_arn, Subject=f"[auto-remediation] {target}: {action}"[:100],
                    Message=json.dumps({"target": target, "problems": incident["problems"], "action": action,
                                        "at": when}, indent=2))
    return action


def handler(event, context, ecs=None, dynamodb=None, sns=None):
    ecs = ecs or boto3.client("ecs")
    table = (dynamodb or boto3.resource("dynamodb")).Table(os.environ["TABLE_NAME"])
    sns = sns or boto3.client("sns")
    topic_arn = os.environ["TOPIC_ARN"]

    failures = []
    for record in event.get("Records", []):
        try:
            action = remediate(json.loads(record["body"]), ecs, table, sns, topic_arn)
            print(json.dumps({"messageId": record["messageId"], "action": action}))
        except Exception as e:  # report per-record so one bad message doesn't retry the whole batch
            print(json.dumps({"messageId": record["messageId"], "error": str(e)}))
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
