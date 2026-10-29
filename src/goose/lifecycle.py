"""Готовность systemd без стороннего SDK и без личных данных в STATUS."""

import os
import socket


def notify_ready():
    address = os.environ.get('NOTIFY_SOCKET')
    if not address:
        return False
    if address.startswith('@'):
        address = '\0' + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
            channel.sendto(b'READY=1\nSTATUS=XMPP connected; archive check finished', address)
        return True
    except OSError:
        return False
