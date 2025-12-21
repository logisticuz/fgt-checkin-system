# api.py
from fastapi import FastAPI
from starlette.middleware.wsgi import WSGIMiddleware

# Import the Dash app (make sure the import path is correct)
from app import app as dash_app

# Create FastAPI app
app = FastAPI()

# Mount Dash at root - dashboard is the main app
app.mount("/", WSGIMiddleware(dash_app.server))

@app.get("/health")
def health_check():
    """
    Health endpoint for the dashboard container.
    Returns status ok if the FastAPI app is alive and Dash is mounted.
    """
    try:
        dash_ok = dash_app is not None and dash_app.server is not None
    except Exception:
        dash_ok = False

    status = "ok" if dash_ok else "degraded"
    return {
        "status": status,
        "components": {
            "fastapi": True,
            "dash": dash_ok
        },
        "version": "1.0.0"
    }
