# Nifty 50 AI Market Prediction & Live Charting Tool

A real-time intraday trading and prediction dashboard for NIFTY 50, featuring dynamic level projections, adaptive risk controls, and automated live candlestick updates.

## Features
- **Live Tick Engine**: Connects to live market data feeds with automatic fallback.
- **AI Strategy Logic**: Incorporates intraday VWAP, previous day POC, 15m HTF trend filters, and midday chop locks.
- **Adaptive Risk Management**: Dynamic Stop-Loss calculations based on ATR with 1:3.2 risk-to-reward targeting.
- **Dual Deployment Architecture**:
  - Run locally via Windows batch scripts (`run.bat` / `start.bat`) with WebSocket streaming.
  - Deploy instantly to Vercel with serverless API handlers (`/api/tick`).

## Quick Start (Local)
1. Double-click `run.bat` or execute:
   ```bash
   python build_and_run.py
   ```
2. Open your browser at `http://localhost:8000/index.html`.

## Deployment (Vercel)
1. Push this repository to GitHub.
2. Import the repository into your Vercel Dashboard.
3. Deploy directly without additional configuration (handled via `vercel.json`).
