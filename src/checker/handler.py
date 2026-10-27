"""Health checker Lambda, run on an EventBridge schedule.

For every target it checks two things: the HTTP endpoint answers 2xx within the
timeout, and the ECS service is running as many tasks as it wants. Unhealthy
targets are queued on SQS for the remediator; every result is published as a
CloudWatch metric so dashboards and alarms see the same signal.
"""
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import boto3

TIMEOUT_SECONDS = float(os.environ.get("CHECK_TIMEOUT_SECONDS", "5"))
METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "AutoRemediation")


def check_http(url, timeout=TIMEOUT_SECONDS):
    """Return (healthy, detail, latency_ms)."""
    start = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            ms = (time.monotonic() - start) * 1000
            return 200 <= resp.status < 300, f"HTTP {resp.status}", ms
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}", (time.monotonic() - start) * 1000
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return False, f"unreachable: {getattr(e, 'reason', e)}", (time.monotonic() - start) * 1000


def check_ecs(ecs, cluster, service):
    """Return (healthy, detail) comparing running vs desired task count."""
    resp = ecs.describe_services(cluster=cluster, services=[service])
    if not resp["services"]:
        return False, "service not found"
    svc = resp["services"][0]
    running, desired = svc["runningCount"], svc["desiredCount"]
    return running >= desired, f"running {running}/{desired}"


def evaluate(target, ecs):
    problems = []
    http_ok, http_detail, latency_ms = check_http(target["url"])
    if not http_ok:
        problems.append(http_detail)
    if target.get("ecs_service"):
        ecs_ok, ecs_detail = check_ecs(ecs, target["ecs_cluster"], target["ecs_service"])
        if not ecs_ok:
            problems.append(ecs_detail)
    return {"target": target["name"], "healthy": not problems, "problems": problems, "latency_ms": round(latency_ms)}


def handler(event, context, ecs=None, sqs=None, cloudwatch=None):
    ecs = ecs or boto3.client("ecs")
    sqs = sqs or boto3.client("sqs")
    cloudwatch = cloudwatch or boto3.client("cloudwatch")
    targets = json.loads(os.environ["TARGETS"])
    queue_url = os.environ["QUEUE_URL"]
    checked_at = datetime.now(timezone.utc).isoformat()

    results = []
    for target in targets:
        result = evaluate(target, ecs)
        results.append(result)
        if not result["healthy"]:
            sqs.send_message(QueueUrl=queue_url, MessageBody=json.dumps({**target, **result, "checked_at": checked_at}))

    cloudwatch.put_metric_data(Namespace=METRIC_NAMESPACE, MetricData=[
        m for r in results for m in (
            {"MetricName": "Healthy", "Dimensions": [{"Name": "Target", "Value": r["target"]}],
             "Value": 1 if r["healthy"] else 0},
            {"MetricName": "LatencyMs", "Dimensions": [{"Name": "Target", "Value": r["target"]}],
             "Value": r["latency_ms"], "Unit": "Milliseconds"},
        )
    ])
    print(json.dumps({"checked_at": checked_at, "results": results}))
    return {"unhealthy": [r["target"] for r in results if not r["healthy"]]}
