output "app_url" {
  value = "http://${aws_lb.app.dns_name}/"
}

output "ecs_cluster" {
  value = aws_ecs_cluster.main.name
}

output "ecs_service" {
  value = aws_ecs_service.app.name
}

output "incident_table" {
  value = aws_dynamodb_table.incidents.name
}

output "dashboard_url" {
  value = "https://${var.region}.console.aws.amazon.com/cloudwatch/home?region=${var.region}#dashboards:name=${var.name}"
}
