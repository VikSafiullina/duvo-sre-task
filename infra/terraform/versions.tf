terraform {
  required_version = ">= 1.9"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.3"
    }
  }

  # Before real use, keep state in GCS (versioned bucket, uniform access):
  # backend "gcs" {
  #   bucket = "<project>-tfstate"
  #   prefix = "duvo-sre-task"
  # }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
