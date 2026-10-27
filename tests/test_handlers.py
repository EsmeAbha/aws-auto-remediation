"""Handler tests with in-memory fakes for AWS clients and a real local HTTP server."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from src.checker import handler as checker
from src.remediator import handler as remediator


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/ok" else 503)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


class FakeECS:
    def __init__(self, running=1, desired=1):
        self.running, self.desired, self.updates = running, desired, []

    def describe_services(self, cluster, services):
        return {"services": [{"runningCount": self.running, "desiredCount": self.desired}]}

    def update_service(self, **kwargs):
        self.updates.append(kwargs)


class FakeSQS:
    def __init__(self):
        self.sent = []

    def send_message(self, QueueUrl, MessageBody):
        self.sent.append(json.loads(MessageBody))


class FakeCloudWatch:
    def __init__(self):
        self.metrics = []

    def put_metric_data(self, Namespace, MetricData):
        self.metrics.extend(MetricData)


class FakeTable:
    def __init__(self):
        self.items = {}

    def get_item(self, Key):
        item = self.items.get((Key["target"], Key["sk"]))
        return {"Item": item} if item else {}

    def put_item(self, Item):
        self.items[(Item["target"], Item["sk"])] = Item


class FakeDynamo:
    def __init__(self):
        self.table = FakeTable()

    def Table(self, name):
        return self.table


class FakeSNS:
    def __init__(self):
        self.published = []

    def publish(self, **kwargs):
        self.published.append(kwargs)


def _env(monkeypatch, targets):
    monkeypatch.setenv("TARGETS", json.dumps(targets))
    monkeypatch.setenv("QUEUE_URL", "q")
    monkeypatch.setenv("TABLE_NAME", "t")
    monkeypatch.setenv("TOPIC_ARN", "arn:topic")


def test_healthy_target_sends_nothing(server, monkeypatch):
    _env(monkeypatch, [{"name": "web", "url": f"{server}/ok", "ecs_cluster": "c", "ecs_service": "s"}])
    sqs, cw = FakeSQS(), FakeCloudWatch()
    out = checker.handler({}, None, ecs=FakeECS(), sqs=sqs, cloudwatch=cw)
    assert out == {"unhealthy": []} and sqs.sent == []
    assert {m["MetricName"]: m["Value"] for m in cw.metrics}["Healthy"] == 1


def test_http_503_and_missing_tasks_are_queued(server, monkeypatch):
    _env(monkeypatch, [{"name": "web", "url": f"{server}/broken", "ecs_cluster": "c", "ecs_service": "s"}])
    sqs = FakeSQS()
    checker.handler({}, None, ecs=FakeECS(running=0, desired=1), sqs=sqs, cloudwatch=FakeCloudWatch())
    assert sqs.sent[0]["problems"] == ["HTTP 503", "running 0/1"]


def test_unreachable_host_is_unhealthy():
    ok, detail, _ = checker.check_http("http://127.0.0.1:1/", timeout=1)
    assert not ok and detail.startswith("unreachable")


def _record(msg_id, **incident):
    body = {"target": "web", "problems": ["HTTP 503"], "ecs_cluster": "c", "ecs_service": "s", **incident}
    return {"messageId": msg_id, "body": json.dumps(body)}


def test_remediator_redeploys_records_and_alerts(monkeypatch):
    _env(monkeypatch, [])
    ecs, ddb, sns = FakeECS(), FakeDynamo(), FakeSNS()
    out = remediator.handler({"Records": [_record("m1")]}, None, ecs=ecs, dynamodb=ddb, sns=sns)
    assert out == {"batchItemFailures": []}
    assert ecs.updates == [{"cluster": "c", "service": "s", "forceNewDeployment": True}]
    assert any(k[1].startswith("INCIDENT#") for k in ddb.table.items)
    assert len(sns.published) == 1


def test_cooldown_suppresses_repeat_redeploys(monkeypatch):
    _env(monkeypatch, [])
    ecs, ddb, sns = FakeECS(), FakeDynamo(), FakeSNS()
    remediator.remediate(json.loads(_record("a")["body"]), ecs, ddb.table, sns, "arn", now=1_000)
    action = remediator.remediate(json.loads(_record("b")["body"]), ecs, ddb.table, sns, "arn", now=1_060)
    assert action == "suppressed (cooldown)"
    assert len(ecs.updates) == 1 and len(sns.published) == 1


def test_bad_message_is_reported_as_partial_failure(monkeypatch):
    _env(monkeypatch, [])
    bad = {"messageId": "bad", "body": "not json"}
    out = remediator.handler({"Records": [bad, _record("good")]}, None,
                             ecs=FakeECS(), dynamodb=FakeDynamo(), sns=FakeSNS())
    assert out == {"batchItemFailures": [{"itemIdentifier": "bad"}]}
