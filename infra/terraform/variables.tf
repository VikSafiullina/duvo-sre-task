variable "project_id" {
  description = "GCP project to deploy into."
  type        = string
}

variable "region" {
  description = "GCP region. EU by default — Duvo customers expect EU data residency."
  type        = string
  default     = "europe-west1"
}

variable "name" {
  description = "Base name for all resources."
  type        = string
  default     = "duvo-app"
}

variable "environment" {
  description = "Deployment environment label (dev/staging/prod)."
  type        = string
  default     = "dev"
}

variable "image" {
  description = "Full container image reference, e.g. europe-west1-docker.pkg.dev/<project>/duvo-app/app:<git-sha>."
  type        = string
}

variable "redis_url" {
  description = "Redis (Memorystore) URL reachable from Cloud Run via VPC access."
  type        = string
  default     = "redis://10.0.0.3:6379/0"
}

variable "otlp_endpoint" {
  description = "OTLP/HTTP collector endpoint. Empty disables trace/log export."
  type        = string
  default     = ""
}

variable "api_min_instances" {
  description = "Minimum API instances (1+ avoids cold starts on the critical path)."
  type        = number
  default     = 1
}

variable "api_max_instances" {
  description = "Maximum API instances — also caps DB connections (max_instances x pool size)."
  type        = number
  default     = 5
}

variable "worker_instances" {
  description = "Worker pool instance count (manual scaling)."
  type        = number
  default     = 1
}

variable "allow_unauthenticated" {
  description = "Expose the API publicly. Keep false unless it is meant to be public."
  type        = bool
  default     = false
}

variable "deletion_protection" {
  description = "Protect Cloud Run resources from terraform destroy."
  type        = bool
  default     = true
}
