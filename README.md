# skyjam — Forecasting GNSS Interference over European Airspace

Module: **I.BA_MLOPS.H2601 · Machine Learning Operations** · Bachelor Informatik, Hochschule Luzern · Autumn Semester 2026.

**The prediction.** For every H3 resolution-2 airspace cell (~180 km edge) over Europe and
adjacent regions, skyjam predicts the probability that the cell will be
**interference-affected 6 hours ahead**. A cell-hour counts as affected when at least 30 %
of the cruise-altitude aircraft in it broadcast a degraded navigation integrity category
(`NIC < 7`) over ADS-B — i.e. the aircraft themselves report that GNSS is not to be
trusted. Success means beating persistence and per-cell hourly climatology on a strictly
out-of-time split (target: PR-AUC ≥ 0.10 above persistence).

There is no historical download for this data: **the feature pipeline is the only source of
history**, which is why it runs hourly from MS1 onwards.

| | |
| --- | --- |
| Proposal (MS1) | [`docs/proposal.pdf`](docs/proposal.pdf) |
| Live URL | _by MS4_ |
| Video pitch | _by MS4_ |
| Status | MS1 · feature pipeline implemented and running hourly; training and inference pipelines are MS3/MS4 |

## Clone and run

```bash
git clone https://github.com/Zayden16/hslu-mlops-hs-26.git
cd hslu-mlops-hs-26
uv sync --locked --all-extras        # pinned via uv.lock
uv run pytest                        # 24 unit tests
cp .env.example .env                 # defaults work; no API key needed

uv run skyjam-ingest                 # poll all 18 points once, write a snapshot
uv run skyjam-features               # build the modelling table from the store
uv run skyjam-backfill               # rebuild the derived layer from raw
```

Or with Docker, which is the same image CI and the scheduled job use:

```bash
docker build -t skyjam .
docker run --rm -v "$PWD/data:/app/data" skyjam        # defaults to skyjam-ingest
```

One sweep takes about two minutes (requests are paced to respect the public API) and
yields roughly 2 500 cruise-level observations across ~70 populated cells. Data is written
under `data/` and is **not** committed.

Rebuild the milestone PDFs (needs pandoc, xelatex, npx): `./docs/build.sh`

## FTI architecture

![FTI architecture](docs/architecture.png)

| Pipeline | Trigger | Responsibility | State |
| --- | --- | --- | --- |
| Feature | hourly GitHub Actions cron | poll 18 ADS-B discs, filter to FL200+, aggregate to H3 cell-hours, label, write; backfill from immutable raw | **running** |
| Training | weekly + on drift | read features, chronological split, train vs. two baselines, track in MLflow, register best | MS3 |
| Inference | hourly + on demand | load the Production model, score live cells, serve the map | MS4 |

Feature semantics (thresholds, altitude floor, H3 resolution, lag set) live in
`src/skyjam/common/schema.py` and are imported by all three pipelines, so training and
serving cannot drift apart.

## Course requirements

- **Format:** individual project, public GitHub repo, kept reachable all semester.
- **Data:** dynamic/live sources only (crypto, weather, SBB delays, …) — no static datasets.
- **Stack:** GitHub, Docker, MLflow or W&B, GitHub Actions or Airflow.
- **Deployment:** cloud deployment is mandatory (GCP / AWS / Hugging Face). USD 50 GCP credits available for students.
- **Grading focus:** the pipeline, not model accuracy. A fully automated pipeline at 60% beats a manual notebook at 99%.

## Milestones and deadlines

| Milestone | Due | Weight | Deliverable |
| --- | --- | --- | --- |
| MS1 Proposal | Thu 01.10.2026, 23:59 (ILIAS) | 10% | max. 2-page PDF, also committed as `docs/proposal.pdf` |
| MS1 Reviews | Thu 08.10.2026 | 10% | 2 anonymous peer reviews |
| MS2 Feature Pipeline | Thu 05.11.2026 | 10% | repo state + `docs/ms2_summary.pdf` |
| MS2 Reviews | Thu 12.11.2026 | 10% | 2 peer reviews |
| MS3 Training Pipeline | Thu 03.12.2026 | 5% | repo state + `docs/ms3_summary.pdf` |
| MS3 Reviews | Thu 10.12.2026 | 5% | 2 peer reviews |
| Oral Exam (Q&A) | Thu 17.12.2026 | 30% | ~15 min individual, on this repo, no presentation |
| MS4 Live System | Sun 10.01.2027 | 20% | code freeze, deployed URL, max. 3 min video pitch, `docs/ms4_summary.pdf` |

Milestones are graded at the **last commit before the deadline**. Tag them: `git tag ms2 && git push --tags`.

### Milestone contents

- **MS1 Proposal:** problem statement (what, horizon, success criterion vs. a baseline), originality & motivation, data source & features (update frequency, label, leakage/split), system design (embedded FTI diagram, stack, repo link).
- **MS2 Feature pipeline:** automated ingestion + backfill, feature engineering & storage, scheduled execution (not manual), Docker image, pinned deps, README.
- **MS3 Training pipeline:** reads MS2 features, reproducible training & evaluation without leakage, MLflow/W&B tracking, model registered, auto-retrain trigger.
- **MS4 Live system:** all 3 pipelines + feature store, scheduled automation and cloud deploy, serving UI, observability (tracking, deployed version, drift monitoring), tests/Docker/pinned deps.

Each milestone summary (max. 2 pages) contains what was achieved **and** a numbered response to every reviewer point (A1, B1, E1 …) with status `Changed · Partly · Rejected · Not yet` and the commit/file.

## Repository layout

```
├── README.md            prediction · FTI diagram · clone-and-run steps · live URL · video link (by MS4)
├── docs/                proposal.pdf, ms2_summary.pdf, ms3_summary.pdf, ms4_summary.pdf, architecture.png
├── src/<package>/
│   ├── features/        feature pipeline (ingest, backfill, compute, write)
│   ├── training/        training pipeline (read features, train, evaluate, register)
│   ├── inference/       inference pipeline (load model, predict, serve)
│   └── common/          shared code (config, IO, feature definitions)
├── ui/                  Streamlit / Gradio front end
├── config/              settings without secrets
├── tests/               unit tests, run in CI
├── notebooks/           exploration only
├── .github/workflows/   scheduled pipelines, CI, deploy
├── Dockerfile           (+ docker-compose.yml for multiple services)
├── pyproject.toml + uv.lock
├── .env.example
└── .gitignore
```

Keep feature definitions in one place shared by training and inference to avoid training–serving skew.

## Rules

- No secrets in the repo or its history. Use `.env` (gitignored) and GitHub Secrets; a leaked key must be rotated.
- No data, model artifacts or `mlruns/` in git.
- Pipelines never import from notebooks.
- AI tools are allowed, but every line and decision must be explainable at the oral exam.
- Individual work; inspiration from mlops-lab.ch and KTH ID2223 is fine, copying is not.

## Course material

`ilias-dump/` holds the ILIAS export: project presentation and guidelines (proposal, repository, milestone summary, review) plus the lecture decks (MLOps intro, infrastructure, model registry/MLflow, processing & prediction modes, stream processing, FTI architecture, data drift, feature stores).

Lecturers: Marc Bravin (module head), Florian Bär, Tobias Mérinat, Josiah Rohrer. Thursdays 09:05–11:25, Rotkreuz S1A_209.
