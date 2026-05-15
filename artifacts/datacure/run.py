import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from app import app
from database.db import init_db

if __name__ == "__main__":
    init_db(app)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
