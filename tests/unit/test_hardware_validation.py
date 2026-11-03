"""Unit tests for system hardware profiling and local CPU embedder initialization."""

from zocrates.rag.embeddings import LOCAL_EMBEDDING_MODEL, LocalEmbedder
from zocrates.rag.hardware import _detect_best_device, get_system_hardware_info
import pytest


@pytest.fixture(autouse=True)
def reset_cached_hw_info(monkeypatch):
    """Ensure _CACHED_HW_INFO is cleanly reset for each test to avoid state leaks."""
    monkeypatch.setattr("zocrates.rag.hardware._CACHED_HW_INFO", None)


def test_get_system_hardware_info():
    """Verify that system RAM is positive and GPU vendor is a known string.

    Asserts that the returned ``SystemHardwareInfo`` has a positive ``ram_gb``
    and that ``gpu_vendor`` is one of the five expected string literals.
    """
    info = get_system_hardware_info()
    assert info.ram_gb > 0.0
    assert info.gpu_vendor in ("nvidia", "apple", "amd", "intel", "cpu")
    d = info.to_dict()
    assert "ram_gb" in d
    assert "gpu_vendor" in d


def test_detect_best_device(monkeypatch):
    """Verify that _detect_best_device falls back to CPU when accelerators are unavailable."""
    import torch

    monkeypatch.setattr("torch.cuda.is_available", lambda: False)
    if hasattr(torch.backends, "mps"):
        monkeypatch.setattr(torch.backends.mps, "is_available", lambda: False)
    dev = _detect_best_device()
    assert dev.type == "cpu"


def test_local_embedder_defaults():
    """Verify LocalEmbedder initializes with the expected model name, dimension, and fallback flag."""
    embedder = LocalEmbedder()
    assert embedder._model_name == "jinaai/jina-embeddings-v5-text-nano"
    assert embedder.dimension == 768
    assert embedder.is_fallback is False


def test_docling_hardware_assessment_and_high_perf(monkeypatch):
    """Verify is_high_performance_gpu and get_docling_hardware_assessment for different GPU profiles."""
    from zocrates.rag.hardware import (
        SystemHardwareInfo,
        get_docling_hardware_assessment,
        is_high_performance_gpu,
    )

    # 1. High performance NVIDIA GPU (>= 6GB VRAM)
    monkeypatch.setattr(
        "zocrates.rag.hardware.get_system_hardware_info",
        lambda: SystemHardwareInfo(
            ram_gb=32.0,
            gpu_vendor="nvidia",
            gpu_name="NVIDIA RTX 4090",
            vram_gb=24.0,
            compute_capability=8.9,
        ),
    )
    assert is_high_performance_gpu() is True
    assessment = get_docling_hardware_assessment()
    assert assessment["recommended_mode"] == "full"
    assert assessment["is_gpu_accelerated"] is True
    assert "Full Mode" in assessment["reason"]

    # 2. Low-VRAM NVIDIA GPU (< 6GB VRAM, e.g. MX250 2GB)
    monkeypatch.setattr(
        "zocrates.rag.hardware.get_system_hardware_info",
        lambda: SystemHardwareInfo(
            ram_gb=16.0,
            gpu_vendor="nvidia",
            gpu_name="NVIDIA GeForce MX250",
            vram_gb=2.0,
            compute_capability=6.1,
        ),
    )
    assert is_high_performance_gpu() is False
    assessment = get_docling_hardware_assessment()
    assert assessment["recommended_mode"] == "fast"
    assert assessment["is_gpu_accelerated"] is False
    assert "Fast Mode" in assessment["reason"]

    # 3. CPU fallback
    monkeypatch.setattr(
        "zocrates.rag.hardware.get_system_hardware_info",
        lambda: SystemHardwareInfo(
            ram_gb=16.0,
            gpu_vendor="cpu",
            gpu_name="Intel Core i7",
            vram_gb=None,
            compute_capability=None,
        ),
    )
    assert is_high_performance_gpu() is False
    assessment = get_docling_hardware_assessment()
    assert assessment["recommended_mode"] == "fast"
    assert assessment["is_gpu_accelerated"] is False
    assert "Fast Mode" in assessment["reason"]

    # 4. NVIDIA GPU with undetermined VRAM (e.g. from procfs / sysfs)
    monkeypatch.setattr(
        "zocrates.rag.hardware.get_system_hardware_info",
        lambda: SystemHardwareInfo(
            ram_gb=16.0,
            gpu_vendor="nvidia",
            gpu_name="NVIDIA GeForce MX250",
            vram_gb=None,
            compute_capability=None,
        ),
    )
    assert is_high_performance_gpu() is False
    assessment = get_docling_hardware_assessment()
    assert assessment["recommended_mode"] == "fast"
    assert assessment["is_gpu_accelerated"] is False
    assert assessment["vram_gb"] is None
    assert "undetermined VRAM" in assessment["reason"]


def test_nvidia_procfs_fallback(monkeypatch, tmp_path):
    """Verify NVIDIA detection falls back to /proc/driver/nvidia/gpus/*/information when nvidia-smi is unavailable."""
    monkeypatch.setattr("sys.platform", "linux")

    # Force subprocess.check_output to fail (nvidia-smi not available)
    def mock_check_output(cmd, *args, **kwargs):
        raise FileNotFoundError("nvidia-smi not found")

    monkeypatch.setattr("subprocess.check_output", mock_check_output)

    # Disable PyTorch CUDA
    import torch
    monkeypatch.setattr("torch.cuda.is_available", lambda: False)

    # Create mock /proc/driver/nvidia/gpus/0000:01:00.0/information
    gpu_proc_dir = tmp_path / "proc" / "driver" / "nvidia" / "gpus" / "0000:01:00.0"
    gpu_proc_dir.mkdir(parents=True)
    info_file = gpu_proc_dir / "information"
    info_file.write_text(
        "Model: \t\t NVIDIA GeForce MX250\n"
        "IRQ:   \t\t 158\n"
        "GPU UUID: \t GPU-3583a2c2-80ac-e317-2f8a-7bc648053a93\n"
        "Video BIOS: \t 86.08.3b.00.03\n"
        "Bus Location: \t 0000:01:00.0\n"
    )

    # Isolated empty sysfs so PCI does not interfere
    empty_sys_dir = tmp_path / "sys"
    empty_sys_dir.mkdir(parents=True)

    monkeypatch.setattr("zocrates.rag.hardware._PROCFS_DIR", str(tmp_path / "proc"))
    monkeypatch.setattr("zocrates.rag.hardware._SYSFS_DIR", str(empty_sys_dir))

    info = get_system_hardware_info(force_refresh=True)
    assert info.gpu_vendor == "nvidia"
    assert info.gpu_name == "NVIDIA GeForce MX250"
    assert info.vram_gb is None
    assert info.compute_capability is None

    from zocrates.rag.hardware import get_docling_hardware_assessment
    assessment = get_docling_hardware_assessment()
    assert assessment["recommended_mode"] == "fast"
    assert assessment["is_gpu_accelerated"] is False
    assert assessment["vram_gb"] is None
    assert "undetermined VRAM" in assessment["reason"]


def test_nvidia_sysfs_pci_fallback(monkeypatch, tmp_path):
    """Verify NVIDIA detection falls back to /sys/bus/pci/devices/* when procfs is not present."""
    monkeypatch.setattr("sys.platform", "linux")

    def mock_check_output(cmd, *args, **kwargs):
        raise FileNotFoundError("CLI tool not found")

    monkeypatch.setattr("subprocess.check_output", mock_check_output)

    import torch
    monkeypatch.setattr("torch.cuda.is_available", lambda: False)

    # Empty procfs
    empty_proc_dir = tmp_path / "proc"
    empty_proc_dir.mkdir(parents=True)
    monkeypatch.setattr("zocrates.rag.hardware._PROCFS_DIR", str(empty_proc_dir))

    # Mock PCI device with vendor 0x10de and class 0x030200 (3D controller)
    pci_dev = tmp_path / "sys" / "bus" / "pci" / "devices" / "0000:01:00.0"
    pci_dev.mkdir(parents=True)
    (pci_dev / "vendor").write_text("0x10de\n")
    (pci_dev / "class").write_text("0x030200\n")

    monkeypatch.setattr("zocrates.rag.hardware._SYSFS_DIR", str(tmp_path / "sys"))

    info = get_system_hardware_info(force_refresh=True)
    assert info.gpu_vendor == "nvidia"
    assert info.gpu_name == "NVIDIA GPU"
    assert info.vram_gb is None
    assert info.compute_capability is None


def test_amd_sysfs_fallback(monkeypatch, tmp_path):
    """Verify AMD detection falls back to /sys/class/drm/card0/device/mem_info_vram_total and product_name."""
    import pytest

    monkeypatch.setattr("sys.platform", "linux")

    # Force subprocess.check_output to fail (nvidia-smi, rocm-smi not available)
    def mock_check_output(cmd, *args, **kwargs):
        raise FileNotFoundError("CLI tool not found")

    monkeypatch.setattr("subprocess.check_output", mock_check_output)

    # Disable PyTorch CUDA
    import torch
    monkeypatch.setattr("torch.cuda.is_available", lambda: False)

    # Isolated empty procfs so nvidia procfs is not detected
    empty_proc_dir = tmp_path / "proc"
    empty_proc_dir.mkdir(parents=True)
    monkeypatch.setattr("zocrates.rag.hardware._PROCFS_DIR", str(empty_proc_dir))

    # Create mock /sys/class/drm/card0/device with AMD vendor and VRAM info
    card_device = tmp_path / "sys" / "class" / "drm" / "card0" / "device"
    card_device.mkdir(parents=True)
    (card_device / "vendor").write_text("0x1002\n")
    (card_device / "product_name").write_text("AMD Radeon RX 7900 XTX\n")
    # 24 GiB total VRAM in bytes
    vram_bytes = 24 * (1024**3)
    (card_device / "mem_info_vram_total").write_text(f"{vram_bytes}\n")

    monkeypatch.setattr("zocrates.rag.hardware._SYSFS_DIR", str(tmp_path / "sys"))

    info = get_system_hardware_info(force_refresh=True)
    assert info.gpu_vendor == "amd"
    assert info.gpu_name == "AMD Radeon RX 7900 XTX"
    assert info.vram_gb == pytest.approx(24.0, rel=1e-2)
    assert info.compute_capability is None

    from zocrates.rag.hardware import get_docling_hardware_assessment, is_high_performance_gpu
    # Without functional torch ROCm runtime, AMD card falls back to Fast Mode
    assert is_high_performance_gpu() is False
    assessment = get_docling_hardware_assessment()
    assert assessment["recommended_mode"] == "fast"
    assert assessment["is_gpu_accelerated"] is False
    assert "24.0 GB VRAM" in assessment["reason"]
    assert "Fast Mode" in assessment["reason"]

    # When torch CUDA/ROCm is functional and reports available
    monkeypatch.setattr("torch.cuda.is_available", lambda: True)
    assert is_high_performance_gpu() is True
    assessment = get_docling_hardware_assessment()
    assert assessment["recommended_mode"] == "full"
    assert assessment["is_gpu_accelerated"] is True
    assert "Full Mode" in assessment["reason"]


def test_intel_sysfs_fallback(monkeypatch, tmp_path):
    """Verify Intel detection falls back to /sys/class/drm/card0/device for Arc / discrete GPUs."""
    import pytest

    monkeypatch.setattr("sys.platform", "linux")

    def mock_check_output(cmd, *args, **kwargs):
        raise FileNotFoundError("CLI tool not found")

    monkeypatch.setattr("subprocess.check_output", mock_check_output)

    import torch
    monkeypatch.setattr("torch.cuda.is_available", lambda: False)

    empty_proc_dir = tmp_path / "proc"
    empty_proc_dir.mkdir(parents=True)
    monkeypatch.setattr("zocrates.rag.hardware._PROCFS_DIR", str(empty_proc_dir))

    card_device = tmp_path / "sys" / "class" / "drm" / "card0" / "device"
    card_device.mkdir(parents=True)
    (card_device / "vendor").write_text("0x8086\n")
    (card_device / "product_name").write_text("Intel Arc A770 Graphics\n")
    # 16 GiB VRAM
    vram_bytes = 16 * (1024**3)
    (card_device / "lmem_total_bytes").write_text(f"{vram_bytes}\n")

    monkeypatch.setattr("zocrates.rag.hardware._SYSFS_DIR", str(tmp_path / "sys"))

    info = get_system_hardware_info(force_refresh=True)
    assert info.gpu_vendor == "intel"
    assert info.gpu_name == "Intel Arc A770 Graphics"
    assert info.vram_gb == pytest.approx(16.0, rel=1e-2)
    assert info.compute_capability is None

