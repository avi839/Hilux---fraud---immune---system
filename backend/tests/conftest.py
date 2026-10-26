import os

from hypothesis import settings

settings.register_profile("default", max_examples=100, deadline=None)
settings.register_profile("ci", max_examples=200, deadline=None, print_blob=True)
settings.register_profile("dev", max_examples=50, deadline=None)
settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "default"))
