from pathlib import Path
import socket

import pytest
import pytest_asyncio

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
