"""Local backend service for the macOS BCI app.

Owns the single Muse connection and runs the app flow
calibration → training → realtime behind a localhost HTTP/WebSocket API.
See backend/README.md for the API contract.
"""
