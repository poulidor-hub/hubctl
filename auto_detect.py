from __future__ import annotations
import json
import subprocess


def detect_server_ip() -> str:
    try:
        out = subprocess.check_output(["hostname", "-I"], text=True)
        ip = out.strip().split()[0]
        if ip:
            return ip
    except (subprocess.CalledProcessError, FileNotFoundError, IndexError):
        pass
    return ""


def lan_cidr(ip: str) -> str:
    parts = ip.rsplit(".", 1)
    if len(parts) == 2:
        return f"{parts[0]}.0/24"
    return "192.168.1.0/24"


def detect_docker_subnet() -> str:
    try:
        out = subprocess.check_output(
            ["docker", "network", "inspect", "hub-network"], text=True
        )
        net = json.loads(out)
        return net[0]["IPAM"]["Config"][0]["Subnet"]
    except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError,
            KeyError, IndexError):
        return "10.42.0.0/16"


def default_whitelist(server_ip: str) -> str:
    subnet = detect_docker_subnet()
    lan = lan_cidr(server_ip)
    return f"{subnet},{lan}"


def default_cors(server_ip: str) -> str:
    return f"https://{server_ip},http://{server_ip}"


