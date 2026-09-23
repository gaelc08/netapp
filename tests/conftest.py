"""Fakes for the three things netapp talks to: ssh (subprocess.run), HTTP
(requests.request, for both ONTAP REST and Cohesity), and the terminal
(input / getpass). Each records what was called in `calls`."""

import getpass
import json
import subprocess
import time

import pytest
import requests

from netapp import rollback


class FakeCompleted:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body
        self.text = "" if body is None else json.dumps(body)
        self.content = self.text.encode()

    def json(self):
        return json.loads(self.text)


class Fakes:
    def __init__(self):
        self.calls = []
        self.ssh_responses = []   # (substring, FakeCompleted), first match wins
        self.http_responses = []  # (method, url-substring, FakeResponse or exception)
        self.inputs = []

    # ssh
    def on_ssh(self, substring, stdout="", stderr="", returncode=0):
        self.ssh_responses.append((substring, FakeCompleted(stdout, stderr, returncode)))

    def run(self, cmd, **kwargs):
        self.calls.append(("ssh", cmd[-1]))
        for substring, result in self.ssh_responses:
            if substring in cmd[-1]:
                return result
        return FakeCompleted()

    # http
    def on_http(self, method, substring, status_code=200, body=None, raises=None):
        self.http_responses.append((method, substring, raises or FakeResponse(status_code, body)))

    def request(self, method, url, **kwargs):
        self.calls.append(("http", method, url, kwargs))
        for m, substring, result in self.http_responses:
            if m == method and substring in url:
                if isinstance(result, Exception):
                    raise result
                return result
        return FakeResponse(404, {"message": "not faked"})

    def ssh_commands(self):
        return [c[1] for c in self.calls if c[0] == "ssh"]

    def http_calls(self):
        return [c for c in self.calls if c[0] == "http"]


@pytest.fixture
def fakes(monkeypatch):
    f = Fakes()
    monkeypatch.setattr(subprocess, "run", f.run)
    monkeypatch.setattr(requests, "request", f.request)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr("builtins.input", lambda prompt="": f.inputs.pop(0) if f.inputs else "")
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "")
    for var in ("COHESITY_APIKEY", "COHESITY_CA_BUNDLE", "ONTAP_PASSWORD", "ONTAP_CERT_FILE", "ONTAP_KEY_FILE"):
        monkeypatch.delenv(var, raising=False)
    rollback.clear()
    yield f
    rollback.clear()


def ontap_fields_output(fields, *rows):
    """Builds "set -showseparator :" + "-fields" output the way ONTAP prints
    it: short-name header, long-name header, then one line per row."""
    header = ":".join(["vserver", *fields])
    long_header = ":".join(["Vserver", *(f.title() for f in fields)])
    return "\n".join([header, long_header, *(":".join(r) for r in rows)]) + "\n"


def cohesity_tree(cluster="damascus-3", svm="svm1", volume="vol1", volume_id=42):
    return [{
        "protectionSource": {"id": 7, "name": cluster, "netappProtectionSource": {"type": "kCluster"}},
        "nodes": [{
            "protectionSource": {"id": 8, "name": svm, "netappProtectionSource": {"type": "kVserver"}},
            "nodes": [{"protectionSource": {"id": volume_id, "name": volume, "netappProtectionSource": {"type": "kVolume"}}}],
        }],
    }]
