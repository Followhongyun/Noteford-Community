"""Per-transfer automatic size preference; manual downloads have no preference cap."""
from contextlib import contextmanager
from contextvars import ContextVar

_config = ContextVar('automatic_download_config', default=None)

@contextmanager
def automatic(config):
    token = _config.set(config)
    try:
        yield
    finally:
        _config.reset(token)

def check_size(size, *, encrypted=False):
    config = _config.get()
    limit = config.get('auto_media_max_mb', 0) if config is not None else 0
    # AES padding is transport overhead, not part of the user's file size.
    overhead = 16 if encrypted else 0
    if limit and size > limit * 1024 * 1024 + overhead:
        from .cdn_download import DownloadError
        raise DownloadError('auto_size_limit')


def ensure_space(directory, remaining=0):
    """Reserve filesystem headroom, without imposing an object-size ceiling."""
    import shutil
    if shutil.disk_usage(directory).free < max(0, remaining) + 64 * 1024 * 1024:
        from .cdn_download import DownloadError
        raise DownloadError('insufficient_space')
