variable "region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "ca-central-1"
}

variable "name" {
  description = "Prefix for every resource name"
  type        = string
  default     = "auto-remediation"
}

variable "app_image" {
  description = "Container image for the demo service"
  type        = string
  default     = "public.ecr.aws/nginx/nginx:stable"
}

variable "alert_email" {
  description = "Email for SNS alerts (confirm the subscription email AWS sends). Empty = no subscription."
  type        = string
  default     = ""
}

variable "check_interval" {
  description = "EventBridge rate, e.g. \"1 minute\" or \"5 minutes\""
  type        = string
  default     = "1 minute"
}

variable "cooldown_minutes" {
  description = "Minimum minutes between automatic redeploys of the same target"
  type        = number
  default     = 10
}

variable "metric_namespace" {
  description = "CloudWatch namespace for checker metrics"
  type        = string
  default     = "AutoRemediation"
}
