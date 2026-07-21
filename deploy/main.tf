# =============================================================================
# Terraform alternative to the gcloud commands in the README.
# Provisions: service account + IAM, Artifact Registry, BigQuery dataset,
# Cloud Run Job, Cloud Scheduler (every 3 months).
#
# The container image must be built/pushed separately (see README step 1);
# var.image points to the finished tag.
# =============================================================================

terraform {
  required_version = ">= 1.7"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.30"
    }
  }
}

variable "project_id" {
  type        = string
  description = "GCP project ID"
}

variable "region" {
  type        = string
  default     = "europe-west3"
  description = "Region for the Cloud Run Job / Scheduler / Artifact Registry"
}

variable "bq_dataset" {
  type        = string
  default     = "kpi_regional"
  description = "BigQuery dataset (must match the job's BQ_DATASET env var)"
}

variable "image" {
  type        = string
  description = "Full image tag, e.g. europe-west3-docker.pkg.dev/PROJECT/kpi-pipeline/regio-kpi:latest"
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# --------------------------------------------------------------- Service Account

resource "google_service_account" "pipeline" {
  account_id   = "kpi-pipeline"
  display_name = "KPI acquisition pipeline (Cloud Run Job)"
}

resource "google_project_iam_member" "bq_data_editor" {
  project = var.project_id
  role    = "roles/bigquery.dataEditor"
  member  = "serviceAccount:${google_service_account.pipeline.email}"
}

resource "google_project_iam_member" "bq_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.pipeline.email}"
}

# The scheduler invokes the job as this SA → needs run.invoker
resource "google_cloud_run_v2_job_iam_member" "scheduler_invoker" {
  name     = google_cloud_run_v2_job.pipeline.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.pipeline.email}"
}

# ------------------------------------------------------------ Artifact Registry

resource "google_artifact_registry_repository" "repo" {
  location      = var.region
  repository_id = "kpi-pipeline"
  format        = "DOCKER"
}

# ----------------------------------------------------------------- BigQuery

resource "google_bigquery_dataset" "kpi" {
  dataset_id  = var.bq_dataset
  location    = "EU"
  description = "External regional KPIs (official/public sources), loaded quarterly"
}

# ------------------------------------------------------------- Cloud Run Job

resource "google_cloud_run_v2_job" "pipeline" {
  name     = "regio-kpi-pipeline"
  location = var.region

  template {
    template {
      service_account = google_service_account.pipeline.email
      max_retries     = 1
      timeout         = "1800s" # 30 min — sources are slow, retries included

      containers {
        image = var.image

        resources {
          limits = {
            cpu    = "1"
            memory = "1Gi" # PDF parsing (pdfplumber) needs some headroom
          }
        }

        env {
          name  = "GCP_PROJECT"
          value = var.project_id
        }
        env {
          name  = "BQ_DATASET"
          value = var.bq_dataset
        }
        env {
          name  = "BQ_LOCATION"
          value = "EU"
        }
        env {
          name  = "LOG_LEVEL"
          value = "INFO"
        }
        # Optional (see README): LDB_NRW_USER/LDB_NRW_PASS for the
        # Landesdatenbank NRW connector — prefer wiring these in via Secret
        # Manager rather than as plain env vars.
      }
    }
  }
}

# ------------------------------------------------------------ Cloud Scheduler
# Every 3 months: 1st of January/April/July/October, 06:00 Europe/Berlin.
# Triggered via the Cloud Run Admin API (v1) using the service account's
# OAuth token.

resource "google_cloud_scheduler_job" "quarterly" {
  name      = "regio-kpi-pipeline-quarterly"
  region    = var.region
  schedule  = "0 6 1 1,4,7,10 *"
  time_zone = "Europe/Berlin"

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.project_id}/jobs/${google_cloud_run_v2_job.pipeline.name}:run"

    oauth_token {
      service_account_email = google_service_account.pipeline.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }

  retry_config {
    retry_count = 1
  }
}

output "service_account_email" {
  value = google_service_account.pipeline.email
}

output "job_name" {
  value = google_cloud_run_v2_job.pipeline.name
}
