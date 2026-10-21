import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVERS = os.path.join(ROOT, "examples", "servers")
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def server_cmd(name):
    return [sys.executable, os.path.join(SERVERS, name)]


def fixture_cmd(name, *args):
    return [sys.executable, os.path.join(FIXTURES, name)] + list(args)


@pytest.fixture
def notes_cmd():
    return server_cmd("notes_server.py")


@pytest.fixture
def buggy_cmd():
    return server_cmd("buggy_server.py")
