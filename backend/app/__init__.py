import logging
import os

from flask import Flask
from flask_cors import CORS


logging.basicConfig(
    level=getattr(logging, os.getenv("LEAGUECLIPS_LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)


def create_app():
    app = Flask(__name__)
    CORS(app)

    from .views import views as main_bp
    app.register_blueprint(main_bp)

    from .scripts.synapse_watcher import start_auto_synapse_watcher

    start_auto_synapse_watcher()

    return app
