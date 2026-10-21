"""trove — Hermes-Plugin-Paket. Oberflaeche: desktop/plugin.js. Backend: dashboard/plugin_api.py."""


def register(ctx):
    """Trove stellt seine Oberflaeche ueber desktop/plugin.js und sein Backend ueber
    dashboard/plugin_api.py bereit. Es registriert keine nativen Tools, Hooks oder Middleware."""
    return None
