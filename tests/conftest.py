# CI has no .env, so provide a non-secret dummy Grid key for worker tests.
import os

os.environ.setdefault("GRID_API_KEY", "test-key")
