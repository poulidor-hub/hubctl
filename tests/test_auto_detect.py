from unittest import mock
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
