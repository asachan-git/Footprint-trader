"""Shared logging setup — IST timestamps, consistent format."""

import logging
import time


class ISTFormatter(logging.Formatter):
    """Log formatter that always outputs timestamps in IST (UTC+5:30)."""
    IST_OFFSET = 5 * 3600 + 30 * 60

    def converter(self, ts):
        return time.gmtime(ts + self.IST_OFFSET)

    def formatTime(self, record, datefmt=None):
        ct = self.converter(record.created)
        if datefmt:
            return time.strftime(datefmt, ct)
        return time.strftime("%Y-%m-%d %H:%M:%S IST", ct)


def setup(level: int = logging.INFO) -> None:
    fmt = ISTFormatter("%(asctime)s %(levelname)s %(message)s")
    handler = logging.StreamHandler()
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    # Silence noisy third-party loggers
    for name in ("metaapi_cloud_sdk", "socketio", "engineio", "werkzeug"):
        logging.getLogger(name).setLevel(logging.WARNING)
