# =============================================================================
# Terraform-Alternative zu den gcloud-Befehlen im README.
# Provisioniert: Service Account + IAM, Artifact Registry, BigQuery-Dataset,
# Cloud Run Job, Cloud Scheduler (alle 3 Monate).
#
# Das Container-Image muss separat gebaut/gepusht werden (siehe README Schritt 1);
# var.image zeigt auf den fertigen Tag.
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
  description = "GCP-Projekt-ID"
}

variable "region" {
  type        = string
  default     = "europe-west3"
  description = "Region für Cloud Run Job / Scheduler / Artifact Registry"
}

variable "bq_dataset" {
  type        = string
  default     = "kpi_regional"
  description = "BigQuery-Dataset (muss zu ENV BQ_DATASET des Jobs passen)"
}

variable "image" {
  type        = string
  description = "Vollständiger Image-Tag, z. B. europe-west3-docker.pkg.dev/PROJEKT/kpi-pipeline/regio-kpi:latest"
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# --------------------------------------------------------------- Service Account

resource "google_service_account" "pipeline" {
  account_id   = "kpi-pipeline"
  display_name = "KPI-Beschaffungs-Pipeline (Cloud Run Job)"
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

# Scheduler ruft den Job mit der Identität dieses SA auf → braucht run.invoker
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
  description = "Externe Regional-KPIs (amtliche/öffentliche Quellen), quartalsweise beladen"
}

# ------------------------------------------------------------- Cloud Run Job

resource "google_cloud_run_v2_job" "pipeline" {
  name     = "regio-kpi-pipeline"
  location = var.region

  template {
    template {
      service_account = google_service_account.pipeline.email
      max_retries     = 1
      timeout         = "1800s" # 30 min — Quellen sind langsam, Retries inklusive

      containers {
        image = var.image

        resources {
          limits = {
            cpu    = "1"
            memory = "1Gi" # PDF-Parsing (pdfplumber) braucht etwas Luft
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
        # Optional (siehe README): LDB_NRW_USER/PASS, BA_*_URL_TEMPLATE —
        # Secrets besser über Secret Manager als env einbinden.
      }
    }
  }
}

# ------------------------------------------------------------ Cloud Scheduler
# Alle 3 Monate: 1. Januar/April/Juli/Oktober, 06:00 Europe/Berlin.
# Trigger via Cloud Run Admin API (v1) mit OAuth-Token des Service Accounts.

resource "google_cloud_scheduler_job" "quarterly" {
  name      = "regio-kpi-pipeline-quartal"
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
