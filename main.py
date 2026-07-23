import sys
import os

# Tell Python to include artifacts/datacure in its module search path
sys.path.insert(0, os.path.abspath("artifacts/datacure"))

# Now import the app from app.py inside artifacts/datacure
from app import app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
