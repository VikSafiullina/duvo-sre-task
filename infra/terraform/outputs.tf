output "api_url" {
  description = "Cloud Run URL of the API."
  value       = google_cloud_run_v2_service.api.uri
}

output "image_repository" {
  description = "Push images here: docker push <this>/app:<tag>."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

output "runtime_service_account" {
  description = "Identity the API and worker run as."
  value       = google_service_account.runtime.email
}
