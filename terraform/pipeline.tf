# ---------- Detection -> queue -> remediation pipeline ----------

locals {
  targets = [{
    name        = "demo-web"
    url         = "http://${aws_lb.app.dns_name}/"
    ecs_cluster = aws_ecs_cluster.main.name
    ecs_service = aws_ecs_service.app.name
  }]
}

resource "aws_sqs_queue" "dlq" {
  name                      = "${var.name}-incidents-dlq"
  message_retention_seconds = 1209600
}

resource "aws_sqs_queue" "incidents" {
  name                       = "${var.name}-incidents"
  visibility_timeout_seconds = 180 # 6x the remediator timeout, per AWS guidance for Lambda consumers
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = 3
  })
}

resource "aws_dynamodb_table" "incidents" {
  name         = "${var.name}-incidents"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "target"
  range_key    = "sk"

  attribute {
    name = "target"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }
}

resource "aws_sns_topic" "alerts" {
  name = "${var.name}-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.alert_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ---------- Lambda packages ----------

data "archive_file" "checker" {
  type        = "zip"
  source_dir  = "${path.module}/../src/checker"
  output_path = "${path.module}/build/checker.zip"
}

data "archive_file" "remediator" {
  type        = "zip"
  source_dir  = "${path.module}/../src/remediator"
  output_path = "${path.module}/build/remediator.zip"
}

# ---------- IAM: least privilege per function ----------

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "checker" {
  name               = "${var.name}-checker"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "checker_logs" {
  role       = aws_iam_role.checker.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "checker" {
  role = aws_iam_role.checker.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "sqs:SendMessage", Resource = aws_sqs_queue.incidents.arn },
      { Effect = "Allow", Action = "ecs:DescribeServices", Resource = aws_ecs_service.app.id },
      # PutMetricData does not support resource-level permissions; scope by namespace instead
      { Effect = "Allow", Action = "cloudwatch:PutMetricData", Resource = "*",
      Condition = { StringEquals = { "cloudwatch:namespace" = var.metric_namespace } } },
    ]
  })
}

resource "aws_iam_role" "remediator" {
  name               = "${var.name}-remediator"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "remediator_logs" {
  role       = aws_iam_role.remediator.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "remediator" {
  role = aws_iam_role.remediator.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"],
      Resource = aws_sqs_queue.incidents.arn },
      { Effect = "Allow", Action = "ecs:UpdateService", Resource = aws_ecs_service.app.id },
      { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:PutItem"], Resource = aws_dynamodb_table.incidents.arn },
      { Effect = "Allow", Action = "sns:Publish", Resource = aws_sns_topic.alerts.arn },
    ]
  })
}

# ---------- Functions and wiring ----------

resource "aws_lambda_function" "checker" {
  function_name    = "${var.name}-checker"
  role             = aws_iam_role.checker.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.checker.output_path
  source_code_hash = data.archive_file.checker.output_base64sha256
  timeout          = 30

  environment {
    variables = {
      TARGETS          = jsonencode(local.targets)
      QUEUE_URL        = aws_sqs_queue.incidents.url
      METRIC_NAMESPACE = var.metric_namespace
    }
  }
}

resource "aws_cloudwatch_event_rule" "schedule" {
  name                = "${var.name}-check-schedule"
  schedule_expression = "rate(${var.check_interval})"
}

resource "aws_cloudwatch_event_target" "checker" {
  rule = aws_cloudwatch_event_rule.schedule.name
  arn  = aws_lambda_function.checker.arn
}

resource "aws_lambda_permission" "events" {
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.checker.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.schedule.arn
}

resource "aws_lambda_function" "remediator" {
  function_name    = "${var.name}-remediator"
  role             = aws_iam_role.remediator.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.remediator.output_path
  source_code_hash = data.archive_file.remediator.output_base64sha256
  timeout          = 30

  environment {
    variables = {
      TABLE_NAME       = aws_dynamodb_table.incidents.name
      TOPIC_ARN        = aws_sns_topic.alerts.arn
      COOLDOWN_MINUTES = tostring(var.cooldown_minutes)
    }
  }
}

resource "aws_lambda_event_source_mapping" "remediator" {
  event_source_arn        = aws_sqs_queue.incidents.arn
  function_name           = aws_lambda_function.remediator.arn
  batch_size              = 5
  function_response_types = ["ReportBatchItemFailures"]
}

# ---------- Alarms: the automation itself is monitored ----------

resource "aws_cloudwatch_metric_alarm" "dlq_not_empty" {
  alarm_name          = "${var.name}-remediation-failing"
  alarm_description   = "Incidents the remediator failed to process 3 times"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.dlq.name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "still_unhealthy" {
  alarm_name          = "${var.name}-demo-web-still-unhealthy"
  alarm_description   = "Auto-remediation did not fix demo-web within 5 checks; a human is needed"
  namespace           = var.metric_namespace
  metric_name         = "Healthy"
  dimensions          = { Target = "demo-web" }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 5
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching" # no data means the checker itself is down
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = var.name
  dashboard_body = jsonencode({
    widgets = [
      { type = "metric", x = 0, y = 0, width = 12, height = 6, properties = {
        title = "demo-web healthy (1 = yes)", region = var.region, stat = "Minimum", period = 60,
      metrics = [[var.metric_namespace, "Healthy", "Target", "demo-web"]] } },
      { type = "metric", x = 12, y = 0, width = 12, height = 6, properties = {
        title = "demo-web latency (ms)", region = var.region, stat = "p95", period = 60,
      metrics = [[var.metric_namespace, "LatencyMs", "Target", "demo-web"]] } },
      { type = "metric", x = 0, y = 6, width = 24, height = 6, properties = {
        title = "Incident queue depth", region = var.region, stat = "Maximum", period = 60,
        metrics = [
          ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.incidents.name],
          ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.dlq.name],
      ] } },
    ]
  })
}
