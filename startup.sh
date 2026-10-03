#!/usr/bin/env bash
# Azure App Service (Linux) startup command for the CareVault dashboard.
#
# App Service passes the public port as $PORT (usually 8000). We bind to
# 0.0.0.0 so the platform's reverse proxy can reach us, and run headless so
# Streamlit doesn't try to open a browser.
#
# Set this script as the "Startup Command" in the Azure portal:
#     bash startup.sh
#
# Required App Setting:  CAREVAULT_DASHBOARD_PG
#   e.g.  host=carevault.postgres.database.azure.com port=5432
#         dbname=carevault user=dashboard_ro password=***** sslmode=require

set -euo pipefail

exec python -m streamlit run dashboard/app.py \
    --server.port="${PORT:-8000}" \
    --server.address=0.0.0.0 \
    --server.headless=true \
    --server.enableCORS=false \
    --browser.gatherUsageStats=false
