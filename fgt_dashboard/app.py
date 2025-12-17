# app.py
from dash import Dash
from layout import create_layout
from callbacks import register_callbacks

# Create Dash instance with URL prefix for embedding in FastAPI under /admin
app = Dash(__name__, requests_pathname_prefix='/admin/')

# Layout is built internally by layout.py (no preloaded args)
app.layout = create_layout()

# Register interactive callbacks
register_callbacks(app)

# Run standalone server only when executing this file directly
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8050, debug=True)
