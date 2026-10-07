import logging

# Tests exercise failure paths on purpose; keep their log lines out of the output.
logging.getLogger("audiolink").addHandler(logging.NullHandler())
logging.getLogger("audiolink").propagate = False
