# CGM Data Inventory

Read-only inventory of the two extracted CGM datasets under `data/`. Numbers computed from source files (AZT1D CSVs) and converted CSVs (Shanghai, converted via `scripts/convert_shanghai.py` into `data/interim/shanghai/`).

- AZT1D root: `data/AZT1D 2025/AZT1D 2025/CGM Records/` — 10.7 MB
- Shanghai T1DM: `data/diabetes_datasets/Shanghai_T1DM/` — 3.2 MB
- Shanghai T2DM: `data/diabetes_datasets/Shanghai_T2DM/` — 9.0 MB

## AZT1D 2025

### 1. Layout

- 25 subject subfolders `Subject 1/` … `Subject 25/`, each containing exactly one CSV file named `Subject N.csv`.
- One file per patient. Total CGM CSV size: 10.7 MB.
- Extra: `README.txt`, `Manuscript.pdf`, `Visual Statistics/` (PNGs — not used).
- Also present: `__MACOSX/` folder (macOS packaging cruft — ignore).

### 2. Columns

Columns intersected across all 25 files (present everywhere):

- `Basal`
- `BolusType`
- `CarbSize`
- `CorrectionDelivered`
- `DeviceMode`
- `EventDateTime`
- `FoodDelivered`
- `TotalBolusInsulinDelivered`

Union of columns across all 25 files (some appear only in some files):

| column | files present in / 25 |
|---|---|
| `Basal` | 25 |
| `BolusType` | 25 |
| `CarbSize` | 25 |
| `CorrectionDelivered` | 25 |
| `DeviceMode` | 25 |
| `EventDateTime` | 25 |
| `FoodDelivered` | 25 |
| `TotalBolusInsulinDelivered` | 25 |
| `CGM` | 24 |
| `Readings (CGM / BGM)` | 1 |

Subject 14 uses `Readings (CGM / BGM)` as the glucose column name instead of `CGM` — every other subject uses `CGM`. See open questions.

Dtypes and 3 sample rows from `Subject 1/Subject 1.csv`:

```
EventDateTime                  object   (e.g. '2023-12-08 00:04:00')
DeviceMode                    float64   (encoded int; mostly NaN)
BolusType                      object   (string, mostly NaN)
Basal                         float64   (U/hr, sparse)
CorrectionDelivered           float64   (U, sparse)
TotalBolusInsulinDelivered    float64   (U, sparse)
FoodDelivered                 float64   (U, sparse)
CarbSize                      float64   (g, sparse)
CGM                             int64   (mg/dL, per README)
```

```
      EventDateTime  DeviceMode BolusType  Basal  CorrectionDelivered  TotalBolusInsulinDelivered  FoodDelivered  CarbSize  CGM
2023-12-08 00:04:00         NaN       NaN    NaN                  NaN                         NaN            NaN       NaN  151
2023-12-08 00:09:00         NaN       NaN    NaN                  NaN                         NaN            NaN       NaN  152
2023-12-08 00:14:00         NaN       NaN    NaN                  NaN                         NaN            NaN       NaN  156
```

Units are not marked in the file — README only names the fields. Glucose values are in mg/dL (range observed 40–400, capped at 400 for several subjects, matches Dexcom G6/G7 clamp).

### 3. Timestamps

- Column: `EventDateTime`.
- Format: naive ISO-like string `YYYY-MM-DD HH:MM:SS`. No timezone in the value; README states all times are **local Arizona time** (MST year-round, no DST).
- Median inter-row interval per subject:

| stat | value |
|---|---|
| min median Δt (s) | 300 |
| max median Δt (s) | 300 |
| overall median Δt (s) | 300 |

Sampling is 5-minute nominal (median Δt = 300 s for all 25 subjects), but intervals are **not** strictly regular. Every subject has some duplicate timestamps and multiple >15 min gaps.

### 4. Per-subject data quality

| subject | rows | days | start | end | med Δt (s) | gaps>15m | max gap (min) | dup ts | glucose col | g min | g max | g missing |
|---|---:|---:|---|---|---:|---:|---:|---:|---|---:|---:|---:|
| Subject 1 | 11042 | 38.99 | 2023-12-08 00:04:00 | 2024-01-15 23:54:00 | 300 | 26 | 600.0 | 161 | `CGM` | 40.0 | 279.0 | 0 |
| Subject 2 | 11194 | 38.99 | 2023-12-10 00:04:56 | 2024-01-17 23:57:16 | 300 | 6 | 145.0 | 5 | `CGM` | 41.0 | 372.0 | 0 |
| Subject 3 | 11605 | 38.99 | 2023-12-09 00:04:00 | 2024-01-16 23:56:00 | 300 | 35 | 1384.0 | 920 | `CGM` | 40.0 | 317.0 | 0 |
| Subject 4 | 12822 | 44.76 | 2023-12-08 00:03:00 | 2024-01-21 18:14:00 | 300 | 7 | 205.0 | 72 | `CGM` | 61.0 | 400.0 | 0 |
| Subject 5 | 13210 | 45.99 | 2023-12-12 00:04:00 | 2024-01-26 23:55:00 | 300 | 25 | 165.0 | 158 | `CGM` | 40.0 | 329.0 | 0 |
| Subject 6 | 12901 | 44.58 | 2023-12-09 00:01:08 | 2024-01-22 13:51:56 | 300 | 4 | 169.9 | 171 | `CGM` | 62.0 | 306.0 | 0 |
| Subject 7 | 13392 | 47.0 | 2023-12-16 00:01:58 | 2024-01-31 23:55:58 | 300 | 12 | 175.0 | 7 | `CGM` | 40.0 | 361.0 | 0 |
| Subject 8 | 8139 | 28.0 | 2024-01-11 23:59:00 | 2024-02-08 23:57:00 | 300 | 4 | 33.0 | 99 | `CGM` | 40.0 | 355.4 | 0 |
| Subject 9 | 10717 | 39.0 | 2024-01-01 00:00:00 | 2024-02-08 23:57:00 | 300 | 21 | 890.0 | 43 | `CGM` | 44.0 | 400.0 | 0 |
| Subject 10 | 12183 | 42.0 | 2023-12-29 00:01:59 | 2024-02-08 23:56:19 | 300 | 16 | 190.0 | 302 | `CGM` | 40.0 | 400.0 | 0 |
| Subject 11 | 12954 | 45.0 | 2024-01-08 00:02:49 | 2024-02-21 23:58:57 | 300 | 6 | 247.7 | 113 | `CGM` | 45.0 | 400.0 | 0 |
| Subject 12 | 12681 | 43.99 | 2024-01-08 00:03:00 | 2024-02-20 23:55:00 | 300 | 12 | 55.0 | 47 | `CGM` | 40.0 | 306.0 | 0 |
| Subject 13 | 12895 | 44.99 | 2024-01-13 00:02:23 | 2024-02-26 23:54:26 | 300 | 6 | 215.0 | 94 | `CGM` | 40.0 | 244.0 | 0 |
| Subject 14 | 13003 | 45.0 | 2024-01-13 00:03:00 | 2024-02-26 23:56:00 | 300 | 8 | 174.0 | 208 | `Readings (CGM / BGM)` | 40.0 | 237.0 | 0 |
| Subject 15 | 15825 | 49.0 | 2024-01-12 00:01:16 | 2024-02-29 23:55:39 | 300 | 7 | 860.0 | 2053 | `CGM` | 40.0 | 400.0 | 0 |
| Subject 16 | 12385 | 42.99 | 2024-01-18 00:04:25 | 2024-02-29 23:57:05 | 300 | 5 | 300.0 | 137 | `CGM` | 40.0 | 342.0 | 0 |
| Subject 17 | 12972 | 44.0 | 2024-01-10 00:01:24 | 2024-02-22 23:57:34 | 300 | 21 | 79.8 | 367 | `CGM` | 40.0 | 400.0 | 0 |
| Subject 18 | 10577 | 37.0 | 2024-02-08 00:01:08 | 2024-03-15 23:56:53 | 300 | 5 | 340.0 | 64 | `CGM` | 40.0 | 340.0 | 0 |
| Subject 19 | 13222 | 46.0 | 2024-02-01 00:00:06 | 2024-03-17 23:58:52 | 300 | 10 | 155.0 | 130 | `CGM` | 40.0 | 293.0 | 0 |
| Subject 20 | 13390 | 46.33 | 2024-02-01 16:03:30 | 2024-03-18 23:54:02 | 300 | 6 | 150.0 | 54 | `CGM` | 44.0 | 318.0 | 0 |
| Subject 21 | 13404 | 46.99 | 2024-02-01 00:03:33 | 2024-03-18 23:54:55 | 300 | 24 | 270.9 | 168 | `CGM` | 42.0 | 400.0 | 0 |
| Subject 22 | 11363 | 40.0 | 2024-02-23 00:01:53 | 2024-04-02 23:56:48 | 300 | 14 | 354.3 | 13 | `CGM` | 40.0 | 305.0 | 0 |
| Subject 23 | 13766 | 48.0 | 2024-02-02 00:01:42 | 2024-03-20 23:54:58 | 300 | 8 | 261.5 | 157 | `CGM` | 40.0 | 311.0 | 0 |
| Subject 24 | 10150 | 35.0 | 2024-03-01 00:00:02 | 2024-04-04 23:57:36 | 300 | 9 | 45.0 | 144 | `CGM` | 40.0 | 283.0 | 0 |
| Subject 25 | 10920 | 38.0 | 2024-03-01 00:03:21 | 2024-04-07 23:58:12 | 300 | 5 | 180.0 | 102 | `CGM` | 41.0 | 362.0 | 0 |

Missing % per column, averaged across all 25 subjects:

| column | mean missing % |
|---|---:|
| `BolusType` | 92.5 |
| `CorrectionDelivered` | 92.5 |
| `TotalBolusInsulinDelivered` | 92.5 |
| `FoodDelivered` | 92.5 |
| `CarbSize` | 92.5 |
| `DeviceMode` | 76.9 |
| `Basal` | 33.5 |
| `EventDateTime` | 0.0 |
| `CGM` | 0.0 |
| `Readings (CGM / BGM)` | 0.0 |

Notes on data quality:

- Every subject has some duplicate `EventDateTime` rows (min 5, max 2053, median 130). Same timestamp can appear on distinct event types (CGM row vs. bolus event vs. basal change), so naive dedupe by timestamp will drop non-glucose events.
- Glucose column has 0 NaN in every subject — that's because non-CGM event rows still carry a value (or the event stream is filtered). Verify.
- Max observed glucose is exactly 400.0 for 8 subjects — device clamp, not a data error.
- Min observed glucose is exactly 40.0 for most subjects — likely the low cap of the sensor.

### 5. Non-glucose signals

All in the same per-subject CSV (no separate files):

- Insulin: `Basal` (U/hr), `CorrectionDelivered`, `TotalBolusInsulinDelivered`, `FoodDelivered` (all U)
- Carbs: `CarbSize` (g)
- Device state: `DeviceMode`, `BolusType`
- No demographics, labs, or medications in the CSVs. Manuscript.pdf may describe cohort characteristics.

### 6. Patient IDs

- Only source: the folder/file name `Subject N`, N ∈ [1, 25]. IDs are consistent (one file per patient). No cross-file join needed.

## Shanghai T1DM & T2DM

### 1. Layout

- `Shanghai_T1DM/`: 16 files, mix of `.xls` (12) and `.xlsx` (4). All BIFF/OLE files are code page 936 (GBK, Simplified Chinese).
- `Shanghai_T2DM/`: 109 files, mix of `.xls` (89) and `.xlsx` (20).
- One file per patient **per visit**, not per patient. Filename pattern: `<PatientID>_<VisitIdx>_<YYYYMMDD>.xls[x]`, where `YYYYMMDD` is the visit start date.
- Two per-cohort summary spreadsheets in the parent directory: `Shanghai_T1DM_Summary.xlsx`, `Shanghai_T2DM_Summary.xlsx`.
- Also present: `__MACOSX/`, `data_analysis.ipynb`, `note.txt` (documents 188 CGM measurements added in Oct 2023 to two T2DM files).

Converted copies (single pass, this session) live in `data/interim/shanghai/{Shanghai_T1DM,Shanghai_T2DM}/*.csv`.

Total unique patients (by filename prefix): T1DM = 12, T2DM = 100. The paper cites 12 T1DM + 100 T2DM — T1DM matches (12 unique patients, 16 files), T2DM has 100 unique patient IDs across 109 files.

Visits per patient:

| visits | T1DM patients | T2DM patients |
|---:|---:|---:|
| 1 | 10 | 92 |
| 2 | 0 | 7 |
| 3 | 2 | 1 |

### 2. Columns (per-visit CGM file)

Column frequency across all Shanghai per-visit CSVs (T1DM `n=16`, T2DM `n=109`). A column that appears in all files has count `= file count`.

| column | T1DM files | T2DM files |
|---|---:|---:|
| `Blood Ketone` | 0/16 | 2/109 |
| `Blood Ketone (mmol / L)` | 16/16 | 107/109 |
| `CBG` | 0/16 | 2/109 |
| `CBG (mg / dl)` | 16/16 | 107/109 |
| `CGM` | 0/16 | 2/109 |
| `CGM (mg / dl)` | 16/16 | 107/109 |
| `CSII - basal insulin` | 0/16 | 2/109 |
| `CSII - basal insulin (Novolin R  IU / H)` | 0/16 | 1/109 |
| `CSII - basal insulin (Novolin R, IU / H)` | 16/16 | 105/109 |
| `CSII - bolus insulin` | 0/16 | 2/109 |
| `CSII - bolus insulin (Novolin R  IU)` | 0/16 | 1/109 |
| `CSII - bolus insulin (Novolin R, IU)` | 16/16 | 106/109 |
| `Date` | 16/16 | 109/109 |
| `Dietary intake` | 16/16 | 109/109 |
| `Insulin dose - i.v.` | 16/16 | 109/109 |
| `Insulin dose - s.c.` | 16/16 | 109/109 |
| `Non-insulin hypoglycemic agents` | 16/16 | 109/109 |
| `胰岛素泵基础量 (Novolin R, IU / H)` | 0/16 | 1/109 |
| `进食量` | 0/16 | 2/109 |
| `饮食` | 16/16 | 107/109 |

Missing % per column, averaged across files (T1DM = mean across 16, T2DM = mean across 109):

| column | T1DM mean missing % | T2DM mean missing % |
|---|---:|---:|
| `Blood Ketone` | - | 100.0 |
| `Blood Ketone (mmol / L)` | 99.7 | 100.0 |
| `CBG` | - | 95.3 |
| `CBG (mg / dl)` | 94.5 | 96.6 |
| `CGM` | - | 0.0 |
| `CGM (mg / dl)` | 0.0 | 0.0 |
| `CSII - basal insulin` | - | 100.0 |
| `CSII - basal insulin (Novolin R  IU / H)` | - | 100.0 |
| `CSII - basal insulin (Novolin R, IU / H)` | 96.9 | 98.4 |
| `CSII - bolus insulin` | - | 100.0 |
| `CSII - bolus insulin (Novolin R  IU)` | - | 100.0 |
| `CSII - bolus insulin (Novolin R, IU)` | 98.3 | 99.2 |
| `Date` | 0.0 | 0.0 |
| `Dietary intake` | 96.5 | 96.6 |
| `Insulin dose - i.v.` | 99.9 | 100.0 |
| `Insulin dose - s.c.` | 98.0 | 99.3 |
| `Non-insulin hypoglycemic agents` | 99.2 | 98.1 |
| `胰岛素泵基础量 (Novolin R, IU / H)` | - | 93.7 |
| `进食量` | - | 96.8 |
| `饮食` | 96.5 | 96.6 |

Sample rows from `1001_0_20210730` (T1DM):

```
                  Date  CGM (mg / dl)  CBG (mg / dl)  Blood Ketone (mmol / L)      Dietary intake   饮食  Insulin dose - s.c. Non-insulin hypoglycemic agents  CSII - bolus insulin (Novolin R, IU)  CSII - basal insulin (Novolin R, IU / H)  Insulin dose - i.v.
0  2021-07-30 16:43:00          113.4            NaN                      NaN                 NaN  NaN                  NaN                             NaN                                   NaN                                       0.3                  NaN
1  2021-07-30 16:58:00          124.2            NaN                      NaN                 NaN  NaN                  NaN                             NaN                                   4.0                                       NaN                  NaN
2  2021-07-30 17:13:00          129.6            NaN                      NaN  data not available  未记录                  NaN                             NaN                                   NaN                                       NaN                  NaN
```

**Glucose units in Shanghai are labelled `mg/dl` but stored at mmol/L resolution.** Every observed CGM value is an exact multiple of `1.8` (= 0.1 mmol/L × 18). Example first-file values: `113.4, 124.2, 129.6, 142.2, 156.6, 162.0, 163.8, 165.6, 169.2, 167.4`. The math is right (39.6 mg/dL = 2.2 mmol/L is a valid severe hypo), but the effective precision is 0.1 mmol/L ≈ 1.8 mg/dL, not 1 mg/dL as AZT1D provides. Don't compare fine-grained variability between the two datasets without accounting for this.

**Duplicate diet column.** Every Shanghai per-visit file carries both `Dietary intake` (English-labelled) and `饮食` (Chinese for "diet"). Content is the same free-text meal description (Chinese-language regardless of column). Treat as one column; drop the duplicate on load.

### 3. Timestamps

- Column: `Date` (some files may spell it `date` — code normalizes case).
- Format: `YYYY-MM-DD HH:MM:SS` after CSV export.
- Timezone: unstated. Files are Chinese hospital records; assume Asia/Shanghai (UTC+8) unless we find otherwise.
- Median Δt per file: T1DM range [900, 900] s; T2DM range [900, 900] s.
- Sampling nominally every 15 min. Intervals not strictly regular — see gap counts below.

### 4. Shanghai T1DM (all 16 files) — per-file profile

| file | rows | days | start | end | med Δt (s) | gaps>30m | max gap (min) | dup ts | g col | g min | g max | g missing |
|---|---:|---:|---|---|---:|---:|---:|---:|---|---:|---:|---:|
| 1001_0_20210730 | 658 | 6.84 | 2021-07-30 16:43:00 | 2021-08-06 12:58:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 48.6 | 307.8 | 0 |
| 1002_0_20210504 | 948 | 9.86 | 2021-05-04 10:33:00 | 2021-05-14 07:18:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 46.8 | 394.2 | 0 |
| 1002_1_20210521 | 933 | 9.72 | 2021-05-21 13:32:00 | 2021-05-31 06:47:00 | 900 | 0 | 30.0 | 0 | `CGM (mg / dl)` | 39.6 | 343.8 | 0 |
| 1002_2_20210909 | 357 | 3.71 | 2021-09-09 16:06:00 | 2021-09-13 09:06:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 39.6 | 219.6 | 0 |
| 1003_0_20210831 | 1339 | 13.94 | 2021-08-31 17:43:00 | 2021-09-14 16:13:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 39.6 | 424.8 | 0 |
| 1004_0_20210425 | 917 | 9.54 | 2021-04-25 17:26:00 | 2021-05-05 06:26:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 68.4 | 327.6 | 0 |
| 1005_0_20210522 | 1256 | 13.07 | 2021-05-22 11:59:00 | 2021-06-04 13:44:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 39.6 | 406.8 | 0 |
| 1006_0_20210114 | 1236 | 12.86 | 2021-01-14 18:39:00 | 2021-01-27 15:24:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 39.6 | 340.2 | 0 |
| 1006_1_20210209 | 1339 | 13.94 | 2021-02-09 13:20:00 | 2021-02-23 11:50:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 39.6 | 448.2 | 0 |
| 1006_2_20210303 | 1135 | 11.81 | 2021-03-03 15:08:00 | 2021-03-15 10:38:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 48.6 | 475.2 | 0 |
| 1007_0_20210726 | 1337 | 13.92 | 2021-07-26 09:58:00 | 2021-08-09 07:58:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 39.6 | 262.8 | 0 |
| 1008_0_20210713 | 766 | 7.97 | 2021-07-13 16:27:00 | 2021-07-21 15:42:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 50.4 | 415.8 | 0 |
| 1009_0_20210803 | 681 | 7.08 | 2021-08-03 12:04:00 | 2021-08-10 14:04:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 68.4 | 347.4 | 0 |
| 1010_0_20210915 | 918 | 9.55 | 2021-09-14 17:30:00 | 2021-09-24 06:45:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 59.4 | 378.0 | 0 |
| 1011_0_20210622 | 536 | 5.57 | 2021-06-22 17:23:00 | 2021-06-28 07:08:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 39.6 | 365.4 | 0 |
| 1012_0_20210923 | 1339 | 13.94 | 2021-09-23 01:08:00 | 2021-10-06 23:38:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 46.8 | 388.8 | 0 |

### 4. Shanghai T2DM (first 30 of 109) — per-file profile

| file | rows | days | start | end | med Δt (s) | gaps>30m | max gap (min) | dup ts | g col | g min | g max | g missing |
|---|---:|---:|---|---|---:|---:|---:|---:|---|---:|---:|---:|
| 2000_0_20201230 | 1339 | 13.94 | 2020-12-30 13:54:00 | 2021-01-13 12:24:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 39.6 | 252.0 | 0 |
| 2001_0_20201102 | 1195 | 12.44 | 2020-11-02 09:40:00 | 2020-11-14 20:10:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 46.8 | 262.8 | 0 |
| 2001_1_20201117 | 1339 | 13.94 | 2020-11-17 09:19:00 | 2020-12-01 07:49:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 55.8 | 268.2 | 0 |
| 2002_0_20210513 | 571 | 5.94 | 2021-05-13 16:22:00 | 2021-05-19 14:52:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 70.2 | 419.4 | 0 |
| 2003_0_20210615 | 669 | 6.96 | 2021-06-15 17:20:00 | 2021-06-22 16:20:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 99.0 | 318.6 | 0 |
| 2004_0_20211028 | 484 | 5.03 | 2021-10-28 14:28:00 | 2021-11-02 15:13:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 59.4 | 329.40000000000003 | 0 |
| 2005_0_20211201 | 691 | 7.19 | 2021-12-01 15:55:00 | 2021-12-08 20:25:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 70.2 | 325.8 | 0 |
| 2006_0_20211112 | 480 | 4.99 | 2021-11-12 15:20:00 | 2021-11-17 15:05:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 102.6 | 282.6 | 0 |
| 2007_0_20220108 | 831 | 8.65 | 2022-01-08 16:46:00 | 2022-01-17 08:16:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 66.60000000000001 | 374.4 | 0 |
| 2008_0_20220118 | 533 | 5.54 | 2022-01-18 16:29:00 | 2022-01-24 05:29:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 54.0 | 286.2 | 0 |
| 2009_0_20211103 | 494 | 5.14 | 2021-11-03 09:43:00 | 2021-11-08 12:58:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 77.39999999999999 | 327.6 | 0 |
| 2010_0_20220111 | 539 | 5.6 | 2022-01-11 17:44:00 | 2022-01-17 08:14:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 63.0 | 403.2 | 0 |
| 2011_0_20220123 | 462 | 4.8 | 2022-01-23 16:58:00 | 2022-01-28 12:13:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 84.60000000000001 | 381.6 | 0 |
| 2012_0_20220126 | 288 | 2.99 | 2022-01-26 12:33:00 | 2022-01-29 12:18:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 57.6 | 252.0 | 0 |
| 2013_0_20220123 | 350 | 3.64 | 2022-01-23 17:00:00 | 2022-01-27 08:15:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 77.39999999999999 | 329.40000000000003 | 0 |
| 2014_0_20201224 | 1333 | 13.88 | 2020-12-24 11:53:00 | 2021-01-07 08:53:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 68.39999999999999 | 196.2 | 0 |
| 2014_1_20210317 | 1339 | 13.94 | 2021-03-17 15:16:00 | 2021-03-31 13:46:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 75.60000000000001 | 203.4 | 0 |
| 2015_0_20210203 | 1339 | 13.94 | 2021-02-03 14:30:00 | 2021-02-17 13:00:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 91.8 | 334.8 | 0 |
| 2015_1_20210219 | 1318 | 13.72 | 2021-02-19 11:58:00 | 2021-03-05 05:13:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 90.0 | 311.40000000000003 | 0 |
| 2016_0_20201224 | 1339 | 13.94 | 2020-12-24 10:27:00 | 2021-01-07 08:57:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 84.6 | 275.4 | 0 |
| 2017_0_20210102 | 1337 | 13.92 | 2021-01-04 09:50:00 | 2021-01-18 07:50:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 73.8 | 340.2 | 0 |
| 2017_1_20201118 | 1337 | 13.92 | 2020-11-18 09:53:00 | 2020-12-02 07:53:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 66.6 | 271.8 | 0 |
| 2018_0_20210420 | 1231 | 12.81 | 2021-04-20 11:33:00 | 2021-05-03 07:03:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 54.0 | 342.0 | 0 |
| 2019_0_20210513 | 751 | 7.81 | 2021-05-13 16:43:00 | 2021-05-21 12:13:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 50.4 | 338.4 | 0 |
| 2020_0_20210423 | 539 | 5.6 | 2021-04-23 18:26:00 | 2021-04-29 08:56:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 73.8 | 284.4 | 0 |
| 2021_0_20211013 | 1135 | 11.81 | 2021-10-13 10:29:00 | 2021-10-25 05:59:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 48.6 | 468.0 | 0 |
| 2022_0_20210419 | 945 | 9.83 | 2021-04-19 11:23:00 | 2021-04-29 07:23:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 77.4 | 293.4 | 0 |
| 2023_0_20210812 | 541 | 5.66 | 2021-08-12 15:07:00 | 2021-08-18 06:52:00 | 900 | 1 | 60.0 | 0 | `CGM (mg / dl)` | 66.6 | 302.4 | 0 |
| 2024_0_20210429 | 729 | 7.58 | 2021-04-29 16:43:00 | 2021-05-07 06:43:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 55.8 | 234.0 | 0 |
| 2025_0_20210506 | 761 | 7.92 | 2021-05-06 09:34:00 | 2021-05-14 07:34:00 | 900 | 0 | 15.0 | 0 | `CGM (mg / dl)` | 93.6 | 358.2 | 0 |

(Full T2DM per-file summary written to `scratch/shanghai_t2dm_files.csv` — 109 rows total.)

Cohort-level data-quality totals:

| stat | T1DM | T2DM |
|---|---:|---:|
| files | 16 | 109 |
| unique patients | 12 | 100 |
| total rows | 15695 | 112475 |
| rows per file: min / median / max | 357 / 940 / 1339 | 247 / 1218 / 1339 |
| days per file: min / median / max | 3.71 / 9.79 / 13.94 | 2.56 / 12.68 / 13.94 |
| files with dup timestamps | 0 | 0 |
| files with any gap > 30 min | 0 | 3 |
| max gap observed (min) | 30.0 | 60.0 |
| glucose range (min / max across files) | 39.6 / 475.2 | 39.6 / 468.0 |
| files with any glucose NaN | 0 | 1 |

### 5. Non-glucose signals in Shanghai per-visit files

Inferred from the observed column set. Column names are verbatim from source (Chinese-language columns exported through pandas `to_csv` in UTF-8):

- CGM glucose: `CGM (mg / dl)` (or `CGM`)
- Capillary/venous fingerstick: `CBG (mg / dl)`, `Blood Ketone (mmol / L)`
- Diet: `Dietary intake` (mixed Chinese/English free text describing meal composition)
- Insulin: `Insulin dose - s.c.` (subcutaneous), `Insulin dose - i.v.` (intravenous)
- Non-insulin hypoglycemic drugs: `Non-insulin hypoglycemic agents`
- CSII (pump) fields: `CSII - bolus insulin (Novolin R, IU)`, `CSII - basal insulin (Novolin R, IU / H)`
- Free-text events: any additional narrative columns

Demographics, labs and medications are **not** in the per-visit files — they live in the summary spreadsheets (`Shanghai_T*DM_Summary.xlsx`, one row per file/visit, 33 columns each):

| summary column (T1DM & T2DM) |
|---|
| `Patient Number` |
| `Gender (Female=1, Male=2)` |
| `Age (years)` |
| `Height (m)` |
| `Weight (kg)` |
| `BMI (kg/m2)` |
| `Smoking History (pack year)` |
| `Alcohol Drinking History (drinker/non-drinker)` |
| `Type of Diabetes` |
| `Duration of Diabetes  (years)` |
| `Acute Diabetic Complications` |
| `Diabetic Macrovascular  Complications` |
| `Diabetic Microvascular Complications` |
| `Comorbidities` |
| `Hypoglycemic Agents` |
| `Other Agents` |
| `Fasting Plasma Glucose (mg/dl)` |
| `2-hour Postprandial Plasma Glucose (mg/dl)` |
| `Fasting C-peptide (nmol/L)` |
| `2-hour Postprandial C-peptide (nmol/L)` |
| `Fasting Insulin (pmol/L)` |
| `2-hour Postprandial Insulin (pmol/L)` |
| `HbA1c (mmol/mol)` |
| `Glycated Albumin (%)` |
| `Total Cholesterol (mmol/L)` |
| `Triglyceride (mmol/L)` |
| `High-Density Lipoprotein Cholesterol (mmol/L)` |
| `Low-Density Lipoprotein Cholesterol (mmol/L)` |
| `Creatinine (umol/L)` |
| `Estimated Glomerular Filtration Rate  (ml/min/1.73m2)` |
| `Uric Acid (mmol/L)` |
| `Blood Urea Nitrogen (mmol/L)` |
| `Hypoglycemia (yes/no)` |

### 6. Patient identification

- Patient ID = first underscore-delimited field of the filename (T1DM `1001..1012`, T2DM `2000..2099`).
- `Patient Number` column in the summary xlsx uses the *full filename stem* (e.g. `1001_0_20210730`), i.e. patient × visit, not just patient. To join summary → CGM file, key on filename stem; to aggregate at patient level, split on `_` and take the first field.
- IDs are consistent across the per-visit file and the summary row (they are literally the same string). No cross-cohort ID collision — T1DM IDs start with `1`, T2DM with `2`.
- One diff: the T1DM summary column is `Duration of Diabetes  (years)` (two spaces before `(`), T2DM is `Duration of diabetes (years)`. Same for `Insulin (pmol/L)` capitalization. These break naive `pd.concat`.

## Cross-dataset comparison

### Overlap and differences

| topic | AZT1D | Shanghai |
|---|---|---|
| unit of analysis | 1 file per patient, ~40 days | 1 file per patient-visit, ~14 days typical |
| sampling | 5 min (Dexcom-style) | 15 min (FreeStyle Libre-style) |
| glucose units | mg/dL, integer 1-unit resolution | mg/dL label but stored at 0.1 mmol/L (~1.8 mg/dL) resolution — every value is `mmol/L × 18` |
| timezone | local Arizona (MST, no DST) | unstated; assume Asia/Shanghai |
| insulin fields | `Basal`, `TotalBolusInsulinDelivered`, `CorrectionDelivered`, `FoodDelivered` (U) | `Insulin dose - s.c.`, `Insulin dose - i.v.`, `CSII - bolus …`, `CSII - basal …` (IU); different route granularity |
| carbs | `CarbSize` (grams, numeric) | `Dietary intake` (free-text meal descriptions, Chinese) |
| device state | `DeviceMode`, `BolusType` (AID / hybrid closed-loop context) | none |
| demographics/labs | none in CSVs | 33-column summary xlsx per cohort |
| patient ID | `Subject N`, integer 1..25 | filename stem `PPPP_V_YYYYMMDD`, patient = first 4 digits |
| cohort | T1D adults on AID | T1D (12) + T2D (100) mixed |

### What would break a naive merge into one `readings(patient_id, ts_utc, glucose_mgdl, ...)` table

1. **Timezone** — AZT1D is Arizona local (UTC-7, no DST); Shanghai is unstated but almost certainly Asia/Shanghai (UTC+8). Concatenating without tz-localize + tz-convert will silently misalign by 15 hours.
2. **Sampling rate** — 5 min vs 15 min. Any per-row model needs a resample step; per-window features (TIR, GMI) are fine but need a `sampling_hz` column.
3. **Patient ID scheme** — integers 1..25 vs strings `1001..2099`. Must be namespaced (e.g. `az:1`, `sh:1001`) or the T1DM/T2DM `1xxx` prefix will collide with any future AZ subject beyond 999.
4. **Patient vs patient-visit granularity** — Shanghai has 1..3 visits per patient with distinct filename stems. If the target table is one row per reading with `patient_id`, Shanghai visits need to be either concatenated or given a `visit_id`.
5. **Column name for glucose** — `CGM` vs `CGM (mg / dl)` vs `Readings (CGM / BGM)` (AZT1D Subject 14). Requires a rename map, not `concat`.
6. **Insulin semantics** — AZT1D `Basal` is U/hr (a rate); Shanghai `CSII - basal insulin (Novolin R, IU / H)` is also U/hr, but AZT1D `TotalBolusInsulinDelivered` (units delivered per event) vs Shanghai `Insulin dose - s.c.` (units per injection) are the same physical thing under different names. `Insulin dose - i.v.` has no AZT1D analogue.
7. **Carbs semantics** — AZT1D `CarbSize` is numeric grams; Shanghai `Dietary intake` is a Chinese-language free-text meal string. Not mergeable without NLP.
8. **Row semantics** — AZT1D rows are event-typed (a row can be a CGM reading, a bolus, or a basal change; most non-CGM columns are NaN). Shanghai rows are ~timestamped snapshots with sparsely populated action columns. Dedupe / event-typing differs — a `pd.concat` without normalization gives inconsistent NaN patterns.
9. **Duplicate timestamps** — present in every AZT1D subject (5..2053 per file) and in some Shanghai files. Root cause differs (AZ = event-per-row; Shanghai = same-minute logging). Merge strategy must handle both.
10. **Character encoding** — Shanghai summary and file content include Chinese characters (code page 936 originals). Convert to UTF-8 (already done for interim CSVs) and stay UTF-8 downstream.
11. **Cohort labeling** — no `diabetes_type` column exists anywhere; must be injected from the source path (`AZT1D` → T1D, `Shanghai_T1DM` → T1D, `Shanghai_T2DM` → T2D).
12. **Glucose resolution mismatch** — AZT1D is 1 mg/dL integer; Shanghai is `mmol/L × 18` (~1.8 mg/dL quantum). Any comparison of within-subject variability (SD, CV, MAGE) must either downsample AZT1D to 0.1 mmol/L or note the resolution asymmetry.
13. **Duplicate content columns in Shanghai** — `Dietary intake` and `饮食` are the same field. Drop-one on load, don't `concat` both.

## Open questions

- **AZT1D `CGM` column has 0 NaNs in every subject** — is every row a CGM reading, or are non-CGM event rows carrying the last-known CGM value? Affects whether we can trust the row as a glucose observation.
- **AZT1D Subject 14** uses `Readings (CGM / BGM)` as the glucose column. Is this a genuine schema variant (BGM fingerstick mixed in) or a rename? Check for a BGM/CGM type flag we might have missed.
- **AZT1D duplicate timestamps** — up to 2053 duplicates for Subject 15. Are these true collisions (same minute, different events) or export bugs? Confirm before choosing a dedupe key.
- **AZT1D `DeviceMode` values** — the enum encoding is not documented in `README.txt`. Need the manuscript or a value-frequency scan to map integers → {normal, sleep, exercise}.
- **Shanghai timezone** — unstated. Confirm it's Asia/Shanghai and not device UTC, since some Libre exports are UTC by default.
- **Shanghai `Dietary intake` language** — parse to grams via a Chinese food ontology, or drop for this project?
- **Shanghai insulin units** — `Novolin R, IU` implies a specific insulin; do we treat all insulin doses as a single scalar (`total_insulin_iu`) or preserve route (`s.c.` / `i.v.` / `CSII`)?
- **Shanghai T2DM patient count** — filename prefixes give a specific number; is any patient double-counted under a different ID? Cross-check `Patient Number` in the summary xlsx against filename stems.
- **Shanghai visit chaining** — for a patient with 3 visits, are the CGM windows contiguous (one long trace split by hospital session) or clearly separated by weeks? Affects whether we merge visits per patient.
- **AZT1D `Basal` units** — README says 'insulin delivery' with no unit. Values look like U/hr (0.5..1.5 range). Confirm from manuscript.
- **Glucose clamp at 400 mg/dL / 40 mg/dL** — treat as censored (right/left) or leave as-is? Matters for GMI / hyper/hypo excursion counts.

## Resolved investigations (feeds schema-proposal.md)

### R1. AZT1D duplicate-timestamp semantics

Reproducible via `python scripts/investigate_az.py`. Per-subject metrics (`grp_all_same_cgm_%` = fraction of duplicate-timestamp groups whose CGM values are all identical; `nonCGM_uniform_%` = fraction of non-CGM column cells in duplicate groups that are equal to the first row of the group):

| subject | dup_ts | dup_rows | grp_all_same_cgm_% | nonCGM_uniform_% |
|---|---:|---:|---:|---:|
| Subject 1 | 161 | 203 | 90.5 | 96.3 |
| Subject 2 | 5 | 10 | 100.0 | 85.7 |
| Subject 3 | 920 | 1045 | 73.6 | 93.6 |
| Subject 4 | 72 | 76 | 100.0 | 92.9 |
| Subject 5 | 158 | 245 | 48.3 | 87.2 |
| Subject 6 | 171 | 179 | 100.0 | 91.1 |
| Subject 7 | 7 | 14 | 100.0 | 69.4 |
| Subject 8 | 99 | 149 | 2.0 | 98.6 |
| Subject 9 | 43 | 68 | 100.0 | 91.4 |
| Subject 10 | 302 | 337 | 100.0 | 95.1 |
| Subject 11 | 113 | 132 | 84.2 | 97.0 |
| Subject 12 | 47 | 73 | 73.1 | 94.5 |
| Subject 13 | 94 | 100 | 100.0 | 92.9 |
| Subject 14 | 208 | 243 | 91.4 | 95.9 |
| Subject 15 | 2053 | 2065 | 83.3 | 92.9 |
| Subject 16 | 137 | 172 | 100.0 | 96.3 |
| Subject 17 | 367 | 498 | 84.7 | 97.5 |
| Subject 18 | 64 | 72 | 87.5 | 96.4 |
| Subject 19 | 130 | 145 | 73.3 | 94.3 |
| Subject 20 | 54 | 68 | 92.9 | 96.9 |
| Subject 21 | 168 | 212 | 86.4 | 99.0 |
| Subject 22 | 13 | 23 | 50.0 | 92.9 |
| Subject 23 | 157 | 168 | 100.0 | 90.9 |
| Subject 24 | 144 | 204 | 98.3 | 98.3 |
| Subject 25 | 102 | 111 | 88.9 | 88.9 |

Two visibly different failure modes in the raw data:

- **Subject 15, ts=`2024-02-02 17:55:59`**: 20 rows, all identical (`Basal=0.750`, `CGM=143`). Same for `21:00:59` (19 rows, all `CGM=96`). This is a **repeated-snapshot export bug** — the pump/CGM writer dumped a state row into the log dozens of times for the same second.
- **Subject 1, ts=`2023-12-09 03:59:00`**: 2 rows, `CGM=122` vs `115` (`Basal` NaN in both). This is **two distinct sensor reads landing in the same second bucket**.

Interpretation:
- Non-CGM columns are ≥85% uniform within dup groups for every subject → duplicates are not carrying independent event payloads. `drop_duplicates(keep="first")` on the full row would kill the "20 identical rows" bug but leave the "two distinct CGM" case as one arbitrary reading.
- CGM values disagree materially in a minority of subjects (Subject 8: only 2% of groups agree — this subject has ~150 dup rows where CGM differs between two same-second reads). Blind drop loses information for these subjects.

**Verdict — one row per 5-min grid point is safe *only with an aggregation rule*, not a naive drop:**
- CGM: median per bucket (robust to the Subject-1 "two-distinct-reads" pattern; identical to any single value for the Subject-15 "20-copies" pattern).
- Insulin/carb numerics (`Basal`, `TotalBolusInsulinDelivered`, `CorrectionDelivered`, `FoodDelivered`, `CarbSize`): sum per bucket (bolus events genuinely accumulate; basal rate collapses to itself when duplicated).
- Categorical (`DeviceMode`, `BolusType`): first non-null.

Blindly calling `drop_duplicates()` on `EventDateTime` would silently lose ~2900 non-redundant readings across the cohort. Aggregation is not optional.

### R2. AZT1D Subject 14 — CGM/BGM type indicator

Subject 14 columns: `EventDateTime, Basal, BolusType, CarbSize, CorrectionDelivered, FoodDelivered, Readings (CGM / BGM), TotalBolusInsulinDelivered, DeviceMode`. Only difference from the other 24 subjects is the glucose column name — no additional flag column, no type field.

`BolusType` values are the same enum as the rest of the cohort (`Standard`, `Standard/Correction`, `Automatic Bolus/Correction`, …) — not a type discriminator.

Cadence and distribution look CGM-only:

- Δt: **97.99 % of intervals are exactly 300 s**. Only 1.6 % ≤ 60 s. No bimodal fingerstick-plus-CGM pattern.
- Glucose values: min=40, median=105, max=237, std=27 — a narrow, clean CGM distribution. No spikes to 300+ that a stressed BGM would show.
- 0 NaN in the glucose column across 13,003 rows.

**Recommendation: keep Subject 14, do not drop.** Rename `Readings (CGM / BGM)` → `cgm_mgdl` on load. The header label suggests the source system supports mixing BGM entries, but this subject's data shows no evidence of it. Dropping 13 k rows (~7% of AZT1D) for a naming quirk with no data-level contamination is overreaction. Add a schema flag (`glucose_source_labeled_bgm_possible=True` for this subject only) and let downstream sanity checks catch anomalies if they appear.

### R3. AZT1D DeviceMode value frequencies

Global value counts across all 25 subjects:

| value | rows | notes |
|---|---:|---|
| `NaN` | 239,283 | overwhelming majority; 5 subjects (1, 5, 7, 11, 22) have 0 non-null DeviceMode at all |
| `sleep` | 60,276 | 20 subjects |
| `0` | 5,262 | **Subject 8 only** — appears nowhere else |
| `exercise` | 1,858 | 9 subjects |
| `sleepsleep` | 33 | **Subject 17 only** — 33 rows, obvious data-entry artefact |

README claims values are `{normal, sleep, exercise}`; the string `normal` never actually appears in the data.

**Proposed mapping** (with confidence noted; not fully documented):

| observed value | maps to | confidence | reasoning |
|---|---|---|---|
| `NaN` | `normal` | medium | README enumerates only three modes; the pump likely writes a mode value only during override (sleep/exercise). Confirm from manuscript. |
| `sleep` | `sleep` | high | literal |
| `exercise` | `exercise` | high | literal |
| `sleepsleep` | `sleep` | high | 33 rows, one subject, string doubled; treat as typo of `sleep` |
| `0` | `unknown` | **low** | 5,262 rows, exclusive to Subject 8. Could be a numeric encoding of `normal` from an earlier firmware, or a distinct state. **Not documented** — leave as `unknown` in the mapping and flag Subject 8 for a schema-level sanity check. Do NOT silently fold into `normal`. |

Bottom line: mapping is best-effort. The `0` case is undocumented; `NaN → normal` is inferred and should be validated against the manuscript before publication.

## How to reproduce

```
python scripts/convert_shanghai.py       # 125 xls/xlsx → data/interim/shanghai/**/*.csv
python scripts/profile_data.py           # prints per-file lines, writes this file
python scripts/investigate_az.py         # answers R1/R2/R3 above
```