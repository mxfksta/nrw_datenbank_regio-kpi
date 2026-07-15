# nrw_datenbank_regio-kpi

Quartalsweise Beschaffung **externer, amtlicher/öffentlicher Regional-KPIs** für
7 Regionen im Kölner/Bonner Raum → Normalisierung → **Google BigQuery**.
Läuft als Container auf **Cloud Run (Job)**, getriggert von **Cloud Scheduler
alle 3 Monate**.

> **Scope:** Ausschließlich externe KPIs. Interne Kennzahlen (Web-/App-Analytics,
> CRM, Newsletter/Push, Social, Vermarktung) sind explizit **nicht** Teil dieses
> Projekts.

| Region | Regionalschlüssel |
|---|---|
| Leverkusen | 05316 |
| Bonn | 05314 |
| Rhein-Sieg-Kreis | 05382 |
| Rhein-Erft-Kreis | 05362 |
| Rheinisch-Bergischer Kreis | 05378 |
| Oberbergischer Kreis | 05374 |
| Kreis Euskirchen | 05366 |

Die konkreten Kennzahlen je Cluster definiert **`kpi_spec.yaml`** (Single Source
of Truth); `regionen.csv` seedet die Dimensionstabelle.

---

## Architektur

```
Cloud Scheduler (cron: 0 6 1 1,4,7,10 *, Europe/Berlin)
        │  POST …/jobs/regio-kpi-pipeline:run  (OAuth, Service Account)
        ▼
Cloud Run JOB  (python:3.12-slim, non-root)
        │  src/main.py: Konnektoren × Regionen, fehlerisoliert
        ▼
Konnektoren (src/connectors/)          Transform (src/transform.py)
  statistik_nrw   Phase 1                aktuellster Wert („aktuell“)
  wahlprofile     Phase 1                Ø 3 neueste Jahre („avg_3j“, nur bei ≥3)
  arbeitsagentur  Phase 1 (konfig.)      Einheiten-/Zahlen-Normalisierung
  5 × Phase-2-Stubs (manuell)
        ▼
BigQuery (src/bq_loader.py)   —   DRY_RUN=1: CSVs nach ./out
  fact_kpi      (MERGE, idempotent; Partition: ingested_at DAY;
                 Clustering: region, kpi_cluster)
  dim_region    (Seed aus regionen.csv)
  pipeline_run  (Lauf-Metadaten: Status, n_rows, n_errors, log_summary)
```

**Warum Cloud Run Job (kein Service)?** Batch-Lauf ohne HTTP-Endpoint; der Job
startet, lädt, beendet sich — kein Idle-Container, sauberes Timeout/Retry-Modell.

**Warum langes Faktenschema?** Eine Zeile = ein Messwert. Neue Kennzahlen/Cluster
brauchen keine Schema-Migration, nur neue Zeilen.

**Warum MERGE statt Append?** Idempotenz: Der fachliche Schlüssel
`(regionalschluessel, kpi_cluster, kennzahl, aggregation, jahr_stichtag)` wird
per MERGE aktualisiert — ein erneuter Lauf im selben Quartal erzeugt keine
Duplikate.

### Datenquellen (Phase 1)

| Quelle | Was | Status |
|---|---|---|
| Kommunalprofil-PDF (`l{RS}.pdf`) | Hauptquelle, alle 7 Regionen: Einwohner (Jahresreihe 2018–2024), Fläche, Altersstruktur + Anteile weiblich/nichtdeutsch, Pendler (Ein-/Aus-/Saldo), Gewerbean-/-abmeldungen, Umsatzsteuer (Reihe), Primär-/verfügbares Einkommen je Einwohner, **Schulen nach Schulform** | **läuft, gegen echte Dateien kalibriert** (Block-Extraktoren auf IT.NRW-Template) |
| Wahlprofil-PDF (`wp{RS}.pdf`) | Wahlbeteiligung + Parteienanteile, jeweils letzte Wahl je Wahlart | **läuft, gegen echte Dateien kalibriert** |
| Zensus 2022 XLSX (`{RS}000_GRUNDINFO_…`) | Einwohner + Anteile zum Zensus-Stichtag 15.05.2022 (Gegenprobe/Basisjahr) | läuft — **nur kreisfreie Städte** (Kreise haben keine Gemeindedatei) |
| BA Arbeitslose + Quoten (bundesweite ZIP) | Arbeitslose (Bestand) + Arbeitslosenquote, neuester Berichtsmonat, für alle 7 Regionen | **läuft** — eine bundesweite Datei (stabile URL) wird pro Lauf einmal geladen und je RS gefiltert; keine Konfiguration nötig (Default-URL, per `BA_EINZELHEFT_ZIP_URL` überschreibbar) |
| Landesdatenbank NRW (GENESIS-REST, ffcsv) | **Wohnen** (Wohnungsbestand, Bau­fertig­stellungen/-genehmigungen), **Tourismus** (Ankünfte, Übernachtungen, Betten), **SV-Beschäftigte nach WZ A–U** (`13111-50i`, inkl. Anteile) und **Kita** (Plätze + betreute Kinder, `22541-01i`) — alle NICHT im Kommunalprofil enthalten | **läuft, live validiert** (Stadt + Kreis); Auth über HTTP-Header, große Tabellen als async Job (Header-Auth + Job-Polling). Zugangsdaten via `LDB_NRW_USER/PASS` |

**Zu `kpi_spec.yaml`:** Die Cluster stehen unter `cluster:`; Kennzahlen sind
dort beschreibende Einträge (z. B. „Altersstruktur (Anteile Altersgruppen)“).
Die Pipeline schreibt in `fact_kpi` die **granularen Ausprägungen** dieser
Einträge (z. B. „Anteil unter 18-Jährige“, „SV-Beschäftigte WZ C“). Um eine
Kennzahl über die Landesdatenbank abzurufen, wird ihr Listeneintrag zu einem
Objekt mit `ldb:`-Block erweitert:

```yaml
  kennzahlen:
  - kennzahl: Einwohner (amtlich)
    einheit: Anzahl
    ldb:
      tabelle: "12411-01i"   # Tabellencode in der Landesdatenbank — verifizieren!
```

**Defensives Parsing:** PDF-/XLSX-Layouts ändern sich. Alle Zuordnungen laufen
über Label-Konstanten (`KOMMUNALPROFIL_LABELS`, `ZENSUS_LABELS`,
`EINZELHEFT_LABELS`); weicht ein Layout grundlegend ab, wirft der Parser einen
`SourceLayoutError` mit Hinweis, welche Konstante zu prüfen ist. Einzelne
fehlende Labels werden geloggt, brechen aber nichts ab.

**Bildung & Betreuung** wurde nach Phase 1 gehoben: Schulen nach Schulform aus
dem Kommunalprofil-PDF, Kita-Plätze + betreute Kinder aus der Landesdatenbank
(beides im `statistik_nrw`-Konnektor). Nur „Bildungsbericht vorhanden" bleibt
qualitativ/manuell.

**Phase 2 (Stubs, bewusst manuell):** Veranstaltungen, Vereine, Mobilität &
Erreichbarkeit, Einzelhandel/Innenstadt, Risiken Starkregen/Hochwasser —
Portale ohne stabile API bzw. qualitative Angaben. Die Konnektor-Gerüste in
`src/connectors/phase2.py` dokumentieren Quellen und TODOs; im Lauf erscheinen
sie als SKIPPED.

---

## Projektstruktur

```
src/
  connectors/
    base.py            # abstrakte Basisklasse + Fehlertaxonomie
    statistik_nrw.py   # Landesdatenbank > Zensus-XLSX > Kommunalprofil-PDF
    landesdatenbank.py # GENESIS-REST-Client (ffcsv), bevorzugter Pfad
    wahlprofile.py     # wp{RS}.pdf: Beteiligung + Parteien je Wahlart
    arbeitsagentur.py  # Einzelheft-XLSX + WZ-CSV (URL-Templates per ENV)
    phase2.py          # dokumentierte Stubs (manuell)
    __init__.py        # Registry
  models.py            # Pydantic: RawObservation, KpiRecord
  transform.py         # aktuell + avg_3j, Zahlen-/Einheiten-Normalisierung
  bq_loader.py         # DDL, Stage+MERGE, DRY_RUN-CSV
  config.py            # Regionen, URL-Templates, ENV-Settings, kpi_spec-Loader
  logging_setup.py     # JSON-Logging (Cloud-Logging-kompatibel)
  net.py               # User-Agent, robots.txt, Rate-Limit, Retries
  main.py              # Orchestrierung, Fehlerisolierung, Exit-Codes
kpi_spec.yaml          # Single Source of Truth der Kennzahlen
regionen.csv           # Region → RS → Typ → Regierungsbezirk
tests/                 # 68+ Tests, komplett offline (Fixtures + responses)
deploy/main.tf         # Terraform-Alternative zu den gcloud-Befehlen
Dockerfile
```

---

## Lokale Entwicklung

```bash
# Python 3.12 + uv (alternativ: pip-tools)
uv venv --python 3.12 .venv && source .venv/bin/activate
uv pip install -r requirements.txt -r requirements-dev.txt

# Konfiguration
cp .env.example .env      # anpassen; ENV-Vars exportieren (z. B. via direnv)

# Tests (kein Netzzugriff)
pytest

# Trockenlauf: schreibt out/fact_kpi.csv, out/dim_region.csv, out/pipeline_run.csv
DRY_RUN=1 python -m src.main
```

Abhängigkeiten ändern: `requirements.in` editieren, dann
`uv pip compile requirements.in -o requirements.txt`
(bzw. `requirements-dev.in` → `requirements-dev.txt`).

### Konfiguration (ausschließlich ENV, 12-Factor)

| Variable | Default | Zweck |
|---|---|---|
| `GCP_PROJECT` | – | Pflicht, sobald `DRY_RUN` aus ist |
| `BQ_DATASET` | `kpi_regional` | Ziel-Dataset |
| `BQ_LOCATION` | `EU` | Dataset-Location |
| `DRY_RUN` | `0` | `1` → CSVs statt BigQuery |
| `OUT_DIR` | `./out` | Zielordner im DRY_RUN |
| `LOG_LEVEL` | `INFO` | Log-Level (JSON-Logs) |
| `HTTP_TIMEOUT_SECONDS` / `HTTP_RATE_LIMIT_SECONDS` | 60 / 1.0 | HTTP-Verhalten |
| `LDB_NRW_USER` / `LDB_NRW_PASS` | – | Landesdatenbank NRW (bevorzugter Pfad) |
| `BA_EINZELHEFT_ZIP_URL` | (stabile Default-URL) | BA Arbeitslose/Quoten; nur überschreiben, um einen fixen Monat statt „Aktuell" zu ziehen |
| `BA_WZ_CSV_URL_TEMPLATE` | – | BA WZ-Export, `{rs}` wird ersetzt (noch zu ermitteln) |

Keine Secrets im Code. Lokal: `GOOGLE_APPLICATION_CREDENTIALS` auf einen
SA-Key zeigen lassen. Auf Cloud Run: Application Default Credentials über die
Service-Account-Identität des Jobs — dort **keinen** Key setzen.

### BigQuery-Schema

`fact_kpi` (partitioniert nach `ingested_at` DAY, geclustert nach `region, kpi_cluster`):

| Spalte | Typ | Beispiel |
|---|---|---|
| region | STRING | "Leverkusen" |
| regionalschluessel | STRING | "05316" |
| kpi_cluster | STRING | "Demografie & Fläche" |
| kennzahl | STRING | "Einwohner" |
| jahr_stichtag | STRING | "2023", "2022-05-15", "2021-2023" (avg_3j) |
| wert | FLOAT64 | 164202.0 |
| einheit | STRING | "Anzahl", "%", "km²", "€" |
| aggregation | STRING | "aktuell" \| "avg_3j" |
| quelle_name / quelle_url | STRING | Herkunft |
| stand_datum | DATE | Abrufdatum |
| run_id | STRING | UUID je Lauf |
| ingested_at | TIMESTAMP | Ladezeitpunkt |

Dazu `dim_region` (Region, RS, Typ, Regierungsbezirk) und `pipeline_run`
(run_id, started_at, finished_at, status, n_rows, n_errors, log_summary).
Tabellen/Dataset werden beim ersten Lauf automatisch angelegt.

**Hinweis Branchenmix:** Der Cluster „Branchenmix“ ist eine Sicht auf
„SV-Beschäftigte nach Wirtschaftszweigen“ (Filter auf `kpi_cluster`) und wird
nicht doppelt materialisiert.

---

## Deployment (Cloud Run Job + Cloud Scheduler)

Variablen für alle Befehle:

```bash
export PROJECT_ID="mein-gcp-projekt"          # <<< anpassen
export REGION="europe-west3"
export REPO="kpi-pipeline"
export IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/regio-kpi:latest"
export JOB="regio-kpi-pipeline"
export SA="kpi-pipeline@${PROJECT_ID}.iam.gserviceaccount.com"
```

### 0. Einmalig: APIs, Service Account, IAM

```bash
gcloud services enable run.googleapis.com cloudscheduler.googleapis.com \
  artifactregistry.googleapis.com bigquery.googleapis.com \
  cloudbuild.googleapis.com --project "$PROJECT_ID"

gcloud iam service-accounts create kpi-pipeline \
  --display-name "KPI-Beschaffungs-Pipeline" --project "$PROJECT_ID"

# Benötigte Rollen des Service Accounts
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member "serviceAccount:${SA}" --role roles/bigquery.dataEditor
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member "serviceAccount:${SA}" --role roles/bigquery.jobUser
```

### 1. Build & Push nach Artifact Registry

```bash
gcloud artifacts repositories create "$REPO" \
  --repository-format docker --location "$REGION" --project "$PROJECT_ID"

gcloud builds submit --tag "$IMAGE" --project "$PROJECT_ID"
```

### 2. Cloud Run **Job** erstellen (nicht Service)

```bash
gcloud run jobs create "$JOB" \
  --image "$IMAGE" \
  --region "$REGION" \
  --service-account "$SA" \
  --memory 1Gi \
  --cpu 1 \
  --task-timeout 30m \
  --max-retries 1 \
  --set-env-vars "GCP_PROJECT=${PROJECT_ID},BQ_DATASET=kpi_regional,BQ_LOCATION=EU,LOG_LEVEL=INFO" \
  --project "$PROJECT_ID"

# Manueller Testlauf:
gcloud run jobs execute "$JOB" --region "$REGION" --project "$PROJECT_ID" --wait
```

Optional zusätzlich setzen (via `--set-env-vars` bzw. Secret Manager):
`LDB_NRW_USER`, `LDB_NRW_PASS`, `BA_EINZELHEFT_URL_TEMPLATE`, `BA_WZ_CSV_URL_TEMPLATE`.

### 3. Cloud Scheduler: alle 3 Monate

Der Scheduler ruft die Cloud-Run-Admin-API mit OAuth-Token des Service Accounts
auf; dafür braucht der SA `roles/run.invoker` auf dem Job:

```bash
gcloud run jobs add-iam-policy-binding "$JOB" \
  --region "$REGION" --project "$PROJECT_ID" \
  --member "serviceAccount:${SA}" --role roles/run.invoker

gcloud scheduler jobs create http "${JOB}-quartal" \
  --location "$REGION" \
  --schedule "0 6 1 1,4,7,10 *" \
  --time-zone "Europe/Berlin" \
  --http-method POST \
  --uri "https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB}:run" \
  --oauth-service-account-email "$SA" \
  --oauth-token-scope "https://www.googleapis.com/auth/cloud-platform" \
  --project "$PROJECT_ID"
```

### 4. IAM-Rollen (Zusammenfassung)

| Principal | Rolle | Zweck |
|---|---|---|
| `kpi-pipeline@…` | `roles/bigquery.dataEditor` | Tabellen anlegen/schreiben |
| `kpi-pipeline@…` | `roles/bigquery.jobUser` | Load-/Query-Jobs (MERGE) |
| `kpi-pipeline@…` | `roles/run.invoker` (auf dem Job) | Scheduler → Job-Trigger |

### Terraform-Alternative

`deploy/main.tf` provisioniert SA + IAM, Artifact Registry, BigQuery-Dataset,
Cloud Run Job und Scheduler:

```bash
cd deploy
terraform init
terraform apply -var project_id="$PROJECT_ID" -var image="$IMAGE"
```

---

## Betrieb

- **Logs:** strukturierte JSON-Zeilen auf stdout → Cloud Logging
  (`severity`, `message`, plus Felder wie `connector`, `region`, `run_id`).
- **Lauf-Historie:** Tabelle `pipeline_run` — `status` ist `success`,
  `partial` (einzelne Quellen fehlgeschlagen) oder `failed`; Details in
  `log_summary`.
- **Fehlerisolierung:** Ein Fehler in einer Quelle/Region bricht den Lauf nie
  ab. Exit-Codes: `0` Daten geladen, `1` Laden fehlgeschlagen (fatal),
  `2` keine einzige Beobachtung beschafft.
- **Höflichkeit:** identifizierender User-Agent, robots.txt wird respektiert,
  Rate-Limit je Host, Retries mit exponentiellem Backoff.

### Landesdatenbank NRW — Betriebshinweise

Der GENESIS-Zugang ist eingerichtet und gegen echte Daten validiert (Wohnen +
Tourismus, kreisfreie Städte **und** Kreise). Wichtig für den Betrieb:

- **Auth:** Zugangsdaten als HTTP-Header (`LDB_NRW_USER`/`LDB_NRW_PASS`); die
  Instanz lehnt Query-/Body-Auth mit „Code 15" ab. Ohne Zugangsdaten wird der
  LDB-Pfad übersprungen und Wohnen/Tourismus bleiben leer.
- **`regionalvariable` ist entscheidend:** Sie schränkt die Extraktion server-
  seitig ein (~13 s statt Timeout). Ohne sie extrahiert GENESIS alle Regionen.
- **Laufzeit:** ~80 s pro Region (mehrere Tabellen), also grob 10 min für alle
  7 Regionen. GENESIS cached Ergebnisse server-seitig → Folgeläufe sind
  schneller. Das Cloud-Run-`--task-timeout` (30 min) deckt das ab.
- **Format:** GENESIS liefert ffcsv-2020 (englische Spalten) als ZIP; der Client
  entpackt und parst das. Neue `ldb:`-Kennzahlen: Wertspalte (`inhalt`) per
  `metadata/table` prüfen; die „Insgesamt"-Zeile hat leere Klassifizierungs-
  Attribute, `%`-Zeilen („Veränderung zum Vorjahr") werden ausgeschlossen.

## Offene Punkte / TODO

1. **Template-Drift beobachten:** Die PDF-Parser sind gegen die echten
   IT.NRW-Templates (Stand 01/2026) kalibriert und gegen echte Fixtures
   getestet. Ändert IT.NRW das Template, liefert ein Block nichts mehr →
   sichtbar im Log ("Blöcke ohne Treffer") bzw. als SourceLayoutError; dann
   Extraktoren in `statistik_nrw.py` nachziehen und Fixtures aktualisieren.
2. **Restliche Phase-2-Cluster** (Veranstaltungen, Vereine, Mobilität,
   Einzelhandel, Risiken): keine amtlich-strukturierte Quelle in den bestehenden
   Verbindungen. Mobilität wäre am ehesten über DB-Stationsdaten hebbar;
   Betreuungsquote (Bildung) ließe sich aus Kita-Plätzen + Kinderzahl je
   Altersgruppe ergänzen.
