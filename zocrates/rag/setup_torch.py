#!/usr/bin/env python3
"""
setup_torch.py — Multi-vendor GPU hardware detection and PyTorch installer.

Supported hardware platforms:
  - NVIDIA CUDA:
      • Compute Capability < 7.5 (e.g., MX250, GTX 10xx sm_61/sm_60/sm_50) -> cu118 wheel index
      • Compute Capability >= 7.5 (e.g., RTX 20xx/30xx/40xx, A100 sm_75+) -> cu121/cu126 wheel index
  - AMD ROCm (Linux):
      • Detects via rocm-smi -> PyTorch ROCm wheel index (https://download.pytorch.org/whl/rocm6.2)
  - Apple Silicon (macOS):
      • Metal Performance Shaders (MPS) -> Standard PyTorch macOS wheel (built-in MPS support)
  - Intel XPU (Arc / Data Center GPUs):
      • Intel OneAPI XPU -> Standard / XPU wheel index
  - Fallback:
      • CPU wheel index
"""

import os
import platform
import subprocess
import sys

os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")


def detect_hardware_vendor() -> tuple[str, str, float | None]:
    """
    Detect the system hardware accelerator vendor.

    Returns:
        A 3-tuple of ``(vendor, name, compute_capability)`` where
        ``vendor`` is one of ``"nvidia"``, ``"amd"``, ``"apple"``,
        ``"intel"``, or ``"cpu"``.
    """
    # 1. Check Apple Silicon (macOS MPS)
    if sys.platform == "darwin" and platform.machine() in ("arm64", "aarch64"):
        return "apple", "Apple Silicon (MPS)", None

    # 2. Check NVIDIA CUDA via nvidia-smi
    try:
        output = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=compute_cap,name", "--format=csv,noheader"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if output:
            line = output.splitlines()[0]
            parts = [p.strip() for p in line.split(",")]
            cc_str = parts[0]
            gpu_name = parts[1] if len(parts) > 1 else "NVIDIA GPU"
            return "nvidia", gpu_name, float(cc_str)
    except Exception:
        pass

    # 3. Check AMD ROCm via rocm-smi / rocminfo
    try:
        output = subprocess.check_output(
            ["rocm-smi", "--showid"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if output:
            return "amd", "AMD Radeon / Instinct (ROCm)", None
    except Exception:
        pass

    # 4. Check Intel GPU via sycl-ls or xpu-smi
    try:
        output = subprocess.check_output(
            ["xpu-smi", "health"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if output:
            return "intel", "Intel Arc / Data Center GPU (XPU)", None
    except Exception:
        pass

    return "cpu", "CPU Only", None


def get_torch_index_url(vendor: str, cc: float | None) -> str | None:
    """Return the appropriate PyTorch wheel index URL for the given vendor and compute capability.

    Args:
        vendor: Accelerator vendor string: ``"nvidia"``, ``"amd"``, ``"apple"``,
            ``"intel"``, or ``"cpu"``.
        cc: NVIDIA compute capability (e.g. ``8.6``), or ``None`` for non-NVIDIA
            or unknown hardware.

    Returns:
        PyTorch ``--index-url`` string, or ``None`` for Apple Silicon
        (standard PyPI wheels include built-in MPS support).
    """
    if vendor == "nvidia":
        if cc is not None and cc < 7.5:
            # Legacy CUDA architectures (Pascal sm_61, Maxwell sm_50, Kepler sm_37)
            return "https://download.pytorch.org/whl/cu118"
        else:
            # Modern CUDA architectures (Turing sm_75, Ampere sm_80/86, Ada sm_89, Hopper sm_90)
            return "https://download.pytorch.org/whl/cu121"
    elif vendor == "amd":
        # AMD ROCm wheel index
        return "https://download.pytorch.org/whl/rocm6.2"
    elif vendor == "apple":
        # Standard PyTorch PyPI wheels for macOS contain native Metal (MPS) support
        return None
    elif vendor == "cpu":
        return "https://download.pytorch.org/whl/cpu"

    return None


def test_torch_acceleration() -> tuple[bool, str]:
    """Test whether PyTorch hardware acceleration is functional in the current environment.

    Runs a minimal tensor operation in an isolated subprocess for each backend
    (CUDA/ROCm, Apple MPS, Intel XPU) to avoid polluting the calling process.

    Returns:
        A 2-tuple of ``(functional, backend_name)`` where ``functional`` is
        ``True`` if any accelerator executed successfully and ``backend_name``
        is a human-readable label (e.g. ``"CUDA GPU"``, ``"CPU"``).
    """
    try:
        import torch

        # Test CUDA (NVIDIA / AMD ROCm)
        if torch.cuda.is_available():
            try:
                res = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "import torch; assert torch.cuda.is_available(); (torch.zeros(1).cuda() + 1).item()",
                    ],
                    capture_output=True,
                    text=True,
                )
                if res.returncode == 0:
                    backend = "ROCm" if getattr(torch.version, "hip", None) else "CUDA"
                    return True, f"{backend} GPU"
            except Exception:
                pass

        # Test Apple Silicon MPS
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            try:
                res = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "import torch; assert torch.backends.mps.is_available(); (torch.zeros(1).to('mps') + 1).item()",
                    ],
                    capture_output=True,
                    text=True,
                )
                if res.returncode == 0:
                    return True, "Apple Silicon (MPS)"
            except Exception:
                pass

        # Test Intel XPU
        if hasattr(torch, "xpu") and torch.xpu.is_available():
            try:
                res = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "import torch; assert torch.xpu.is_available(); (torch.zeros(1).to('xpu') + 1).item()",
                    ],
                    capture_output=True,
                    text=True,
                )
                if res.returncode == 0:
                    return True, "Intel XPU"
            except Exception:
                pass

    except Exception:
        pass

    return False, "CPU"


def main() -> None:
    """Detect system hardware and install the appropriate PyTorch wheel.

    Performs the following steps:
      1. Detect the GPU vendor (NVIDIA, AMD, Apple, Intel, or CPU-only).
      2. Select the matching PyTorch wheel index URL.
      3. If the current PyTorch build is already functional, exit early.
      4. Otherwise install the appropriate wheel via ``uv pip install``.
      5. Re-test acceleration and report the outcome.
    """
    print("🔍 Detecting system hardware accelerator vendor...")
    vendor, gpu_name, cc = detect_hardware_vendor()

    if vendor != "cpu":
        print(f"✨ Detected Hardware: {gpu_name} (Vendor: {vendor.upper()}" + (f", Compute Capability: {cc}" if cc else "") + ")")
    else:
        print("ℹ️ No discrete GPU accelerator detected.")

    index_url = get_torch_index_url(vendor, cc)
    if index_url:
        print(f"📦 Selected PyTorch wheel index: {index_url}")
    else:
        print("📦 Using standard PyPI wheels (includes built-in accelerator support).")

    # Check current PyTorch status
    functional, active_backend = test_torch_acceleration()
    if functional:
        print(f"✅ Current PyTorch installation is fully functional with {active_backend} acceleration! No changes needed.")
        return

    print("⚡ Installing matching PyTorch build via uv...")
    cmd = ["uv", "pip", "install", "--upgrade", "torch", "torchvision", "torchaudio"]
    if index_url:
        cmd.extend(["--index-url", index_url])

    print(f"Running: {' '.join(cmd)}")
    res = subprocess.run(cmd)

    if res.returncode == 0:
        print("\n🧪 Testing newly installed PyTorch build...")
        functional, active_backend = test_torch_acceleration()
        if functional:
            print(f"🎉 Success! PyTorch hardware acceleration ({active_backend}) is working on your system!")
        else:
            print(
                "⚠️ PyTorch installed, but GPU execution test failed. "
                "The backend will gracefully fall back to CPU mode."
            )
    else:
        print("❌ Failed to install PyTorch wheel.")
        sys.exit(res.returncode)


if __name__ == "__main__":
    main()
