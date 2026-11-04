# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 POULIDOR Audit & Développement (Valérian Poulidor)

"""Tests de l'installer debug_agent."""
import pytest
from unittest.mock import patch, MagicMock

from hubctl_cli.debug_agent import installer


class TestDetectGPU:

    def test_detect_gpu_returns_none_when_no_nvidia(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = installer.detect_gpu()
            assert result is None

    def test_detect_gpu_returns_dict_when_nvidia_present(self):
        fake = MagicMock()
        fake.stdout = "NVIDIA GeForce RTX 3090, 24576 MiB, 23223 MiB\n"
        fake.returncode = 0
        with patch("subprocess.run", return_value=fake):
            result = installer.detect_gpu()
            assert result is not None
            assert result["name"] == "NVIDIA GeForce RTX 3090"
            assert result["vram_mb"] == 24576

    def test_detect_gpu_returns_none_on_nvidia_smi_error(self):
        fake = MagicMock()
        fake.returncode = 1
        fake.stderr = "command not found"
        with patch("subprocess.run", return_value=fake):
            result = installer.detect_gpu()
            assert result is None


class TestCheckOllama:

    def test_check_ollama_returns_version_when_installed(self):
        fake = MagicMock()
        fake.stdout = "ollama version is 0.6.2\n"
        fake.returncode = 0
        with patch("subprocess.run", return_value=fake):
            result = installer.check_ollama()
            assert result is not None
            assert "0.6" in result

    def test_check_ollama_returns_none_when_not_installed(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = installer.check_ollama()
            assert result is None


class TestCheckModel:

    def test_check_model_returns_true_when_present(self):
        fake = MagicMock()
        fake.stdout = "NAME           SIZE     MODIFIED\ngranite3.2:8b  5.0 GB   2 days ago\n"
        fake.returncode = 0
        with patch("subprocess.run", return_value=fake):
            assert installer.check_model("granite3.2:8b") is True

    def test_check_model_returns_false_when_absent(self):
        fake = MagicMock()
        fake.stdout = "NAME          SIZE     MODIFIED\nllama3:8b     4.7 GB   3 days ago\n"
        fake.returncode = 0
        with patch("subprocess.run", return_value=fake):
            assert installer.check_model("granite3.2:8b") is False


class TestRecommendModel:

    def test_recommend_8b_when_gpu_has_enough_vram(self):
        with patch("hubctl_cli.debug_agent.installer.detect_gpu",
                   return_value={"name": "RTX 3090", "vram_mb": 24576}):
            assert installer.recommend_model() == "8b"

    def test_recommend_2b_when_gpu_low_vram(self):
        with patch("hubctl_cli.debug_agent.installer.detect_gpu",
                   return_value={"name": "GTX 1050", "vram_mb": 4096}):
            assert installer.recommend_model() == "2b"

    def test_recommend_2b_when_no_gpu(self):
        with patch("hubctl_cli.debug_agent.installer.detect_gpu",
                   return_value=None):
            assert installer.recommend_model() == "2b"


class TestVerifyReady:

    def test_verify_ready_ok(self):
        with patch("hubctl_cli.debug_agent.installer.check_ollama", return_value="0.6.2"), \
             patch("hubctl_cli.debug_agent.installer.check_model", return_value=True):
            ok, msg = installer.verify_ready("granite3.2:8b")
            assert ok is True

    def test_verify_ready_no_ollama(self):
        with patch("hubctl_cli.debug_agent.installer.check_ollama", return_value=None), \
             patch("hubctl_cli.debug_agent.installer.check_model", return_value=True):
            ok, msg = installer.verify_ready("granite3.2:8b")
            assert ok is False
            assert "ollama" in msg.lower()

    def test_verify_ready_no_model(self):
        with patch("hubctl_cli.debug_agent.installer.check_ollama", return_value="0.6.2"), \
             patch("hubctl_cli.debug_agent.installer.check_model", return_value=False):
            ok, msg = installer.verify_ready("granite3.2:8b")
            assert ok is False
            assert "modele" in msg.lower() or "model" in msg.lower()
