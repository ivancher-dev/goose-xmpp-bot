import errno

import pytest
from omemo.session_manager import BundleUploadFailed
from slixmpp import Iq
from slixmpp.exceptions import IqError, IqTimeout

from goose.retry import temporary_xmpp_error


@pytest.mark.parametrize('condition,expected', [
    ('service-unavailable', True), ('remote-server-timeout', True),
    ('item-not-found', False), ('forbidden', False), ('not-authorized', False)])
def test_iq_failure_classification_uses_condition_not_message(condition, expected):
    iq = Iq()
    iq['type'], iq['error']['condition'] = 'error', condition
    assert temporary_xmpp_error(IqError(iq)) is expected


def test_known_omemo_network_wrapper_is_retryable_but_storage_and_protocol_errors_are_not():
    wrapped = BundleUploadFailed('private payload must never be logged')
    wrapped.__cause__ = IqTimeout(Iq())
    assert temporary_xmpp_error(wrapped)
    for error in (PermissionError(errno.EACCES, 'permission'), ValueError('invalid keys'),
                  RuntimeError('timeout in private text')):
        wrapped.__cause__ = error
        assert not temporary_xmpp_error(wrapped)
    assert temporary_xmpp_error(OSError(errno.ENETUNREACH, 'network'))
