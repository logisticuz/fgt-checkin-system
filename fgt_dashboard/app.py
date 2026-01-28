# app.py
from dash import Dash
try:
    # When running in Docker (files copied flat to /app/)
    from layout import create_layout
    from callbacks import register_callbacks
except ImportError:
    # When running locally with package structure
    from fgt_dashboard.layout import create_layout
    from fgt_dashboard.callbacks import register_callbacks

# Create Dash instance (mounted at root in production)
# suppress_callback_exceptions=True allows callbacks to reference elements
# that don't exist at startup (needed for dynamic layout)
app = Dash(__name__, requests_pathname_prefix='/', suppress_callback_exceptions=True)

# Layout as function reference (not called) = regenerated on each page load
# This allows the event dropdown to show updated events from Airtable
# without requiring a container rebuild
app.layout = create_layout

# Register interactive callbacks
register_callbacks(app)

# Run standalone server only when executing this file directly
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8050, debug=True)
