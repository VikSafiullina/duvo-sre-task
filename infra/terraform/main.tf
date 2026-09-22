locals {
  apis = toset([
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "secretmanager.googleapis.com",
  ])

  otel_env = var.otlp_endpoint == "" ? tomap({
    OTEL_TRACES_EXPORTER  = "none"
    OTEL_LOGS_EXPORTER    = "none"
    OTEL_METRICS_EXPORTER = "none"
    }) : tomap({
    OTEL_EXPORTER_OTLP_ENDPOINT = var.otlp_endpoint
    OTEL_EXPORTER_OTLP_PROTOCOL = "http/protobuf"
    OTEL_TRACES_EXPORTER        = "otlp_proto_http"
    OTEL_LOGS_EXPORTER          = "otlp_proto_http"
    OTEL_METRICS_EXPORTER       = "none"
  })

  common_env = merge(local.otel_env, {
    ENVIRONMENT              = var.environment
    REDIS_URL                = var.redis_url
    OTEL_RESOURCE_ATTRIBUTES = "deployment.environment.name=${var.environment}"
  })
}

resource "google_project_service" "apis" {
  for_each           = local.apis
  service            = each.value
  disable_on_destroy = false
}

resource "google_artifact_registry_repository" "images" {
  repository_id = var.name
  location      = var.region
  format        = "DOCKER"

  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 20
    }
  }

  depends_on = [google_project_service.apis]
}

# One runtime identity with only what it needs: read one secret, write telemetry.
resource "google_service_account" "runtime" {
  account_id   = "${var.name}-run"
  display_name = "${var.name} Cloud Run runtime"
}

resource "google_project_iam_member" "runtime_telemetry" {
  for_each = toset([
    "roles/cloudtrace.agent",
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.runtime.email}"
}

# Terraform manages the secret container only. The value is added out-of-band
# (`gcloud secrets versions add ...`) so credentials never land in Terraform state.
resource "google_secret_manager_secret" "database_url" {
  secret_id = "${var.name}-database-url"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "runtime_reads_database_url" {
  secret_id = google_secret_manager_secret.database_url.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_cloud_run_v2_service" "api" {
  name                = "${var.name}-api"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = var.deletion_protection

  template {
    service_account                  = google_service_account.runtime.email
    max_instance_request_concurrency = 80
    timeout                          = "30s"

    scaling {
      min_instance_count = var.api_min_instances
      max_instance_count = var.api_max_instances
    }

    containers {
      image = var.image

      ports {
        container_port = 8000
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }

      dynamic "env" {
        for_each = merge(local.common_env, {
          SERVICE_NAME                      = "${var.name}-api"
          OTEL_SERVICE_NAME                 = "${var.name}-api"
          OTEL_PYTHON_FASTAPI_EXCLUDED_URLS = "healthz,readyz,metrics" # no trace per probe
        })
        content {
          name  = env.key
          value = env.value
        }
      }

      env {
        name = "DATABASE_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.database_url.secret_id
            version = "latest"
          }
        }
      }

      startup_probe {
        period_seconds    = 2
        failure_threshold = 15
        http_get {
          path = "/healthz"
        }
      }

      liveness_probe {
        period_seconds = 15
        http_get {
          path = "/healthz"
        }
      }
    }
  }

  depends_on = [
    google_project_service.apis,
    google_secret_manager_secret_iam_member.runtime_reads_database_url,
  ]
}

resource "google_cloud_run_v2_service_iam_member" "public_invoker" {
  count    = var.allow_unauthenticated ? 1 : 0
  name     = google_cloud_run_v2_service.api.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# Worker pools are Cloud Run's primitive for pull-based, always-on background work:
# no HTTP port, no request-driven CPU throttling.
resource "google_cloud_run_v2_worker_pool" "worker" {
  name                = "${var.name}-worker"
  location            = var.region
  deletion_protection = var.deletion_protection

  scaling {
    scaling_mode          = "MANUAL"
    manual_instance_count = var.worker_instances
  }

  template {
    service_account = google_service_account.runtime.email

    containers {
      image   = var.image
      command = ["opentelemetry-instrument"]
      args    = ["python", "-m", "app.worker"]

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }

      dynamic "env" {
        for_each = merge(local.common_env, {
          SERVICE_NAME                          = "${var.name}-worker"
          OTEL_SERVICE_NAME                     = "${var.name}-worker"
          OTEL_PYTHON_DISABLED_INSTRUMENTATIONS = "redis" # arq polling is trace noise
        })
        content {
          name  = env.key
          value = env.value
        }
      }

      env {
        name = "DATABASE_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.database_url.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  depends_on = [
    google_project_service.apis,
    google_secret_manager_secret_iam_member.runtime_reads_database_url,
  ]
}
