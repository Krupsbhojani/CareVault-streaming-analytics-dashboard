@echo off
cd /d "C:\kRUSHIL\laptop\KRUSHIL\K\Sem-3\Big Data\DSA508 Carevault PROJECT"
set "CAREVAULT_PROCESSOR_PG=host=carevault-pg-kb.postgres.database.azure.com port=5432 dbname=carevault user=processor_role password=CareVault2026Proc sslmode=require"
"C:\Users\kruvn\anaconda3\python.exe" processor/worker.py --shard 0 --num-shards 1