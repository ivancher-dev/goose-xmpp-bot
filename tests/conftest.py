from pathlib import Path
from copy import deepcopy
import socket
import xml.etree.ElementTree as ET

import pytest
import pytest_asyncio
from slixmpp import Message

from goose.config import load_config
from goose.storage import Database


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Обычные тесты не должны обращаться в сеть")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


@pytest.fixture
def config_file(tmp_path):
    root = Path(__file__).resolve().parents[1]
    text = (root / "config/config.example.toml").read_text()
    directory = tmp_path / "data"
    directory.mkdir(mode=0o700)
    text = text.replace('/var/lib/goose-xmpp-bot', str(directory))
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


@pytest.fixture
def config(config_file):
    return load_config(config_file)


@pytest_asyncio.fixture
async def database(config):
    db = Database(config.data_dir, config.jid)
    await db.initialize(config.users)
    return db


@pytest.fixture
def carbon_message(config):
    def wrap(inner, *, sender=None, recipient=None, direction="received"):
        outer = Message()
        outer["from"] = sender if sender is not None else config.jid
        outer["to"] = recipient if recipient is not None else f"{config.jid}/{config.resource}"
        outer["type"] = "chat"
        carbon = ET.SubElement(outer.xml, f"{{urn:xmpp:carbons:2}}{direction}")
        forwarded = ET.SubElement(carbon, "{urn:xmpp:forward:0}forwarded")
        forwarded.append(deepcopy(inner.xml))
        return outer
    return wrap
