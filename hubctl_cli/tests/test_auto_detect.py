# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

from unittest import mock
import subprocess
from hubctl_cli import auto_detect


def test_lan_cidr():
    assert auto_detect.lan_cidr("192.0.2.10") == "192.168.1.0/24"
    assert auto_detect.lan_cidr("10.0.0.1") == "10.0.0.0/24"


def test_default_whitelist():
    with mock.patch.object(auto_detect, "detect_docker_subnet", return_value="10.42.0.0/16"):
        wl = auto_detect.default_whitelist("192.0.2.10")
    assert "10.42.0.0/16" in wl
    assert "192.168.1.0/24" in wl


def test_default_cors():
    origins = auto_detect.default_cors("192.0.2.10")
    assert "https://192.0.2.10" in origins
    assert "http://192.0.2.10" in origins


def test_detect_docker_subnet_swallows_timeout():
    """docker network inspect peut se suspendre si le daemon Docker est dégradé.
    L'appel doit être borné par un timeout et retomber sur le défaut, pas geler
    le boot."""
    err = subprocess.TimeoutExpired(cmd=["docker"], timeout=10)
    with mock.patch.object(auto_detect.subprocess, "check_output", side_effect=err):
        assert auto_detect.detect_docker_subnet() == "10.42.0.0/16"


def test_detect_server_ip_swallows_timeout():
    err = subprocess.TimeoutExpired(cmd=["hostname"], timeout=5)
    with mock.patch.object(auto_detect.subprocess, "check_output", side_effect=err):
        assert auto_detect.detect_server_ip() == ""
