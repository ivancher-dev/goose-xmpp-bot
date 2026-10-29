"""Классификация временных сетевых ошибок без разбора личного текста исключений."""

import errno

from omemo.session_manager import (BundleDownloadFailed, BundleUploadFailed,
                                   DeviceListDownloadFailed, DeviceListUploadFailed)
from slixmpp.exceptions import IqError, IqTimeout


def temporary_xmpp_error(error):
    # OMEMO оборачивает IQ-ошибки; распознаём только известные сетевые оболочки.
    for _ in range(8):
        if isinstance(error, (TimeoutError, IqTimeout, ConnectionError)):
            return True
        if isinstance(error, IqError):
            return error.iq['error']['condition'] in {
                'service-unavailable', 'remote-server-timeout', 'resource-constraint', 'internal-server-error'}
        if isinstance(error, OSError):
            return error.errno in {errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN, errno.ETIMEDOUT}
        if not isinstance(error, (BundleDownloadFailed, BundleUploadFailed,
                                  DeviceListDownloadFailed, DeviceListUploadFailed)):
            return False
        error = error.__cause__
    return False
